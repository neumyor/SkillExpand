"""Optional native-engine smoke: run with the configured ALFWORLD_PYTHON.

Set EXPE_NATIVE_ALFWORLD_SMOKE=1. No model/network. Exercises real engine
reset/observations and the Skill-aware L1 runner with scripted model responses.
"""
import json
import os
from pathlib import Path
from skillexpand.l1 import learning as L
from types import SimpleNamespace
import unittest


@unittest.skipUnless(os.environ.get('EXPE_NATIVE_ALFWORLD_SMOKE') == '1',
                     'Requires the dedicated ALFWorld venv and native data')
class NativeAlfworldSmoke(unittest.TestCase):
    def test_native_wrapper_skill_round_and_resume(self):
        import tempfile
        from unittest.mock import patch
        from skillexpand.runtime import agent_factory as F
        from skillexpand.l1.experience import gather_task_experience
        from skillexpand.l1.audit import audit_checkpoint
        from skillexpand.l1.adapters import resolve
        from skillexpand import schema as S
        from langchain.schema import HumanMessage
        cfg = F.load_config('alfworld')
        cfg.benchmark.general.use_cuda = False
        cfg.agent.llm = 'gpt-3.5-turbo'
        skill = S.Skill('alfworld.native', 'native', 0, 'Native', 'Household tasks',
                        'Use exact observed object IDs.', S.Provenance(rationale='native smoke'))
        class Model:
            def __init__(self):
                self.outputs = iter(['> look', '> look'])
                self.prompts = []
            def __call__(self, messages, **kwargs):
                self.prompts.append(messages)
                if any("The task's execution is finished." in m.content for m in messages):
                    return json.dumps({'lessons': [], 'diagnosis': '', 'uncertainty': 'No method established.'})
                return next(self.outputs)
        model = Model()
        with tempfile.TemporaryDirectory() as directory, patch.object(F, 'LLM_CLS', return_value=model):
            path = Path(directory)/'task.json'
            exp, agent = gather_task_experience(cfg, 0, 'native', 'train', skill=skill,
                selected_skill_id=skill.skill_id, evolution_round=2,
                max_attempts=1, supervised_repair=False, checkpoint_path=path)
            try:
                self.assertEqual(exp.evolution_round, 2)
                data = json.loads(path.read_text())
                self.assertEqual(data['trials'][0]['termination'], 'repeated_action')
                self.assertTrue(agent.env.truncated)
                audit_checkpoint(data, resolve(cfg))
                self.assertTrue(any(skill.body in m.content for m in model.prompts[0]))
                before = path.read_bytes()
                calls = len(model.prompts)
                same, second = gather_task_experience(cfg, 0, 'native', 'train', skill=skill,
                    selected_skill_id=skill.skill_id, evolution_round=2,
                    max_attempts=1, supervised_repair=False, checkpoint_path=path)
                second.env.close()
                self.assertEqual(same, exp)
                self.assertEqual(len(model.prompts), calls)
                self.assertEqual(path.read_bytes(), before)
                agent.env.reset()
                self.assertFalse(agent.env.truncated)
                self.assertIsNone(agent.env.termination_reason)
                from skillexpand.runtime import parallel as PL
                spec = PL.UnitSpec('native-final', 'alfworld', 0, S.ROLE_EVAL,
                    S.ARM_EVAL, S.MODE_CONSOLIDATED_DIRECT, 'none',
                    skill_key=skill.key, skill_body=skill.body)
                with patch.object(F, 'LLM_CLS', return_value=Model()), patch.object(PL, '_config', return_value=cfg):
                    final = PL.execute_fixed(spec)
                self.assertIsNone(final['error'])
                self.assertFalse(final['success'])
                self.assertEqual(final['failure_mode'], 'repeated_action')
                self.assertEqual(final['steps'], 2)
                self.assertFalse(final['events'][-1]['environment']['success'])
            finally:
                agent.env.close()

    def test_two_resets_feedback_and_no_guidance(self):
        import yaml
        import alfworld.agents.environment as environment
        from skillexpand.l1.adapters import AlfworldAdapter
        from skillexpand.l1 import protocol as P
        root=Path(__file__).resolve().parents[1]
        os.environ.setdefault('ALFWORLD_DATA',str(root/'data'/'alfworld'))
        if not os.environ.get('ALFWORLD_CONFIG') or not os.environ.get('ALFWORLD_BENCH_SRC'):
            self.fail('Set ALFWORLD_CONFIG and ALFWORLD_BENCH_SRC before native smoke')
        import skillexpand
        config_path=Path(skillexpand.__file__).parent/'configs'/'benchmark'/'alfworld.yaml'
        config=yaml.safe_load(config_path.read_text())
        config['general']['use_cuda']=False
        task_file=Path(os.environ.get('EXPE_TASK_FILE',str(root/'data/alfworld/alfworld_tasks_suffix.json')))
        row=json.loads(task_file.read_text())[0]
        main=environment.get_environment(config['env']['type'])(config,train_eval=config['split'])
        main.game_files=[str(root/row['gamefile'])]
        env=main.init_env(batch_size=1)
        adapter=AlfworldAdapter()
        trials=[]
        initial=[]
        try:
            for i in range(1,3):
                obs,info=env.reset()
                initial.append(obs[0])
                obs,reward,done,info=env.step(['look'])
                agent=SimpleNamespace(env=SimpleNamespace(
                    _admissible_commands=info['admissible_commands'][0]))
                trial=dict(index=i,phase='autonomous',status='completed',
                    success=bool(info['won'][0]),termination='step_budget',
                    events=[dict(ref='e1',action='look',observation=obs[0])])
                trial['feedback']=adapter.build_feedback(agent,trial)
                trials.append(trial)
                self.assertIsNone(adapter.prepare_guidance(agent,trials))
                self.assertEqual(trial['feedback']['admissible_actions'],info['admissible_commands'][0])
            self.assertEqual(initial[0],initial[1])
            state=P.refresh({},trials)
            self.assertEqual(state['latest_attempt']['trial'],2)
            self.assertIn('reset',state['latest_attempt']['feedback']['reset_note'])
            card=L.card(0,row['goal'],trials,state,None,'smoke-task-0',len)
            self.assertNotIn('guidance',card)
            self.assertFalse(card['execution']['success'])
            self.assertTrue(all(t['phase'] == 'autonomous' for t in card['execution']['trials']))
        finally:
            env.close()

if __name__=='__main__':
    unittest.main()
