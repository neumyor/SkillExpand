"""Offline L1 integration tests: real QA environment/agent, scripted model only."""
import json
import tempfile
import unittest
from pathlib import Path
from skillexpand.l1 import learning as L
from unittest.mock import patch
from omegaconf import OmegaConf
from skillexpand.runtime import agent_factory as F
from skillexpand import schema as S
from skillexpand.l1.agent import RepairAgent
from skillexpand.l1.adapters import ContextIndex
from skillexpand.l1.adapters import Adapter
from skillexpand.l1.adapters import register
from skillexpand.l1.runner import run
from skillexpand.l1 import protocol as P
from skillexpand.l2 import editor as ED
from skillexpand.runtime import parallel as PL

SUPERVISED_DIAGNOSIS = json.dumps({
    'diagnosis': {'kind': 'knowledge_or_interpretation_gap', 'reason': 'Missing maker information.'},
    'evidence_refs': ['task'],
    'next_change': {'instruction': 'Search Prius then submit the supported maker.',
                    'actions': ['Finish[Toyota]']},
    'uncertainty': 'Unaided capability is not established.',
    'guidance_delta': 'The reference supplies Toyota.'})

class Model:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.prompts = []
        self.synthesis = json.dumps({'claims': []})
        self.repair_synthesis = None
    def __call__(self, messages, **kwargs):
        self.prompts.append('\n'.join(m.content for m in messages))
        if 'Repair only the rejected final-extraction claims.' in self.prompts[-1]:
            value = self.repair_synthesis if self.repair_synthesis is not None else self.synthesis
        elif "The task's execution is finished." in self.prompts[-1]:
            value = self.synthesis
        else:
            value = next(self.outputs)
        if isinstance(value, Exception):
            raise value
        return value


class L1RepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.tasks = self.root / 'tasks.json'
        self.tasks.write_text(json.dumps([{'question': 'Gas up its Prius', 'answers': ['Toyota'],
            'context': '[DOC] [TLE] Prius [PAR] Toyota makes the Prius. [DOC] [TLE] Mars [PAR] Mars is red.'}]))
        self.cfg = F.load_config('searchqa')
        self.cfg.benchmark.task_file = str(self.tasks)
        self.cfg.agent.llm = 'gpt-3.5-turbo'
        self.cfg.benchmark.max_steps = 4
        self.skill = S.Skill(skill_id='searchqa.family-p002', family_id='family-p002',
            version=3, name='lookup', description='Find an entity using supplied evidence.', body='Use supplied evidence.',
            provenance=S.Provenance(rationale='test'))
    def tearDown(self):
        self.tmp.cleanup()
    def agent(self, outputs):
        model = Model(outputs)
        with patch.object(F, 'LLM_CLS', lambda **kwargs: model):
            a = F.build_agent(self.cfg, task_idx=0, rules=self.skill.body,
                              agent_cls=RepairAgent, fewshot_strategy='none',
                              openai_api_key='EMPTY')
        a.train()
        return a, model
    def gather(self, agent, k=1, supervised=True, path=None):
        return run(agent, self.cfg, 0, 'family-p001', S.SPLIT_SOURCE, self.skill, 0,
                   self.skill.skill_id, S.SELECTION_AGENT, 'agent_choice', '',
                   k=k, supervised=supervised, checkpoint_path=path)[0]
    def test_experience_public_entrypoint(self):
        from skillexpand.l1.experience import gather_task_experience
        model = Model(['Action 1: Search[Prius]', 'Action 2: Finish[Toyota]'])
        with patch.object(F, 'LLM_CLS', lambda **kwargs: model):
            exp, agent = gather_task_experience(self.cfg, 0, 'family-p001', 'source',
                skill=self.skill, selected_skill_id=self.skill.skill_id,
                max_attempts=1, checkpoint_path=self.root / 'public.json')
        self.assertTrue(exp.reward)
        self.assertEqual(exp.experience_card['execution']['trials'][0]['phase'], 'autonomous')
        self.assertEqual(len(exp.trial_rewards),1)

    def test_alfworld_feedback_does_not_invent_goal_state(self):
        from types import SimpleNamespace
        from skillexpand.l1.adapters import AlfworldAdapter
        adapter = AlfworldAdapter()
        agent = SimpleNamespace(env=SimpleNamespace(_admissible_commands=['take apple 1 from table 1']))
        feedback = adapter.build_feedback(agent, {'success':False, 'termination':'step_budget',
            'events':[{'observation':'You see apple 1.'}]})
        self.assertFalse(feedback['won'])
        self.assertIn('unknown', feedback['reset_note'])
        self.assertIsNone(adapter.prepare_guidance(agent, []))
        self.assertFalse(adapter.repeated_action_is_stalled([{'action':'look'}], 'look'))

    def test_panel_explorer_is_not_modified(self):
        from skillexpand.benchmarks.searchqa import QAEnv
        from skillexpand.benchmarks.searchqa import ContextExplorer
        env = QAEnv('q','a',context='[DOC] first [DOC] second')
        self.assertIsInstance(env.explorer, ContextExplorer)
        self.assertEqual(env.explorer.search('first'),env.explorer.search('second'))

    def test_searchqa_reset_clears_context_and_search_failure_is_bounded(self):
        from skillexpand.benchmarks.searchqa import QAEnv

        env = QAEnv('q', 'answer', context='[DOC] first')
        env.explorer.search('first')
        env.reset()
        observation, *_ = env.step('Lookup[first]')
        self.assertIn('last page Searched was not found', observation)

        class Broken:
            def search(self, _):
                raise RuntimeError('offline')
            def reset(self):
                pass
        env.explorer = Broken()
        with self.assertRaisesRegex(RuntimeError, 'offline'):
            env.step('Search[first]')

    def test_resume_signature_ignores_reconstruction_metadata(self):
        a,m=self.agent(['Action 1: Finish[Toyota]'])
        p=self.root/'resume.json'
        self.gather(a,path=p)
        import dataclasses
        self.skill=dataclasses.replace(self.skill, provenance=S.Provenance(rationale='reconstructed again'))
        a,m=self.agent([])
        self.assertTrue(self.gather(a,path=p).reward)
        self.assertEqual(m.prompts,[])

    def test_custom_adapter_registration_and_config(self):
        from skillexpand.l1.adapters import resolve
        class Custom(Adapter):
            execution_instructions = 'custom'
            def build_feedback(self, agent, trial):
                return ['benchmark-specific', trial['success']]
        register('custom',Custom)
        cfg=OmegaConf.create({'benchmark':{'name':'custom','l1':{'reflection_instructions':'repair custom'}}})
        adapter=resolve(cfg)
        self.assertEqual(adapter.execution_instructions,'custom')
        self.assertEqual(adapter.reflection_instructions,'repair custom')
        self.assertEqual(adapter.build_feedback(None,{'success':True}),['benchmark-specific',True])

    def test_error_resume_preserves_trial_budget_without_false_failure(self):
        a,m=self.agent([RuntimeError('transport')])
        p=self.root/'partial.json'
        with self.assertRaisesRegex(RuntimeError,'transport'):
            self.gather(a,k=2,path=p)
        a,m=self.agent(['Action 1: Finish[Toyota]'])
        exp=self.gather(a,k=2,path=p)
        self.assertTrue(exp.reward)
        self.assertEqual(len(json.loads(p.read_text())['trials']),2)
        self.assertEqual(exp.trial_rewards,(True,))
        self.assertEqual(len(exp.experience_card['execution']['trials']),1)

    def test_interrupted_observations_do_not_enter_final_card(self):
        agent,_=self.agent(['Action 1: Search[Prius]',RuntimeError('transport')])
        path=self.root/'interrupted-evidence.json'
        with self.assertRaisesRegex(RuntimeError,'transport'):
            self.gather(agent,k=1,path=path)
        agent,_=self.agent(['Action 1: Finish[Toyota]'])
        exp=self.gather(agent,k=1,path=path)
        data=json.loads(path.read_text())
        self.assertEqual([t['index'] for t in data['trials']],[1,2])
        self.assertEqual([t['index'] for t in data['synthesis']['input']['attempts']],[2])
        self.assertTrue(all(not row['id'].startswith('t1:') for row in exp.experience_card['evidence']))

    def test_timeout_or_provider_interruption_does_not_consume_autonomous_budget(self):
        a,m=self.agent([RuntimeError('timeout')])
        p=self.root/'timeout.json'
        with self.assertRaisesRegex(RuntimeError,'timeout'):
            self.gather(a,k=1,path=p)
        a,m=self.agent(['Action 1: Finish[Toyota]'])
        exp=self.gather(a,k=1,path=p)
        self.assertTrue(exp.reward)
        self.assertEqual(exp.trial_phases,('autonomous',))
        self.assertEqual(len(exp.experience_card['execution']['trials']),1)


    def test_exact_k_budget_and_malformed_reflection_fallback(self):
        a,m=self.agent(['Action 1: Finish[Honda]', 'bad json',
                       'Action 1: Finish[Ford]', 'bad json',
                       'Action 1: Finish[GM]', SUPERVISED_DIAGNOSIS, 'Action 1: Finish[Toyota]'])
        exp=self.gather(a,k=3)
        self.assertEqual(exp.trial_phases,('autonomous',)*3+('supervised',))
        execution=[p for p in m.prompts if 'Answer a SearchQA clue' in p]
        self.assertEqual(len(execution),4)
        self.assertTrue(all('Reference answer:' not in p for p in execution[:3]))
        self.assertIn('Reference answer: Toyota',execution[3])
        self.assertEqual(exp.experience_card['execution']['trials'][-1]['phase'],'supervised')

    def test_local_search_and_lookup(self):
        x = ContextIndex('[DOC] Toyota makes Prius. [DOC] Mars is red.')
        self.assertIn('Toyota', x.search('Prius'))
        self.assertNotIn('Mars', x.search('Prius'))
        self.assertIn('Mars', x.search('Mars'))
        self.assertIn('Toyota', x.lookup('D1|Toyota'))
        self.assertIn('No matching', x.search('xyz'))
        x.reset()
        self.assertIn('No matching', x.lookup('Mars'))
    def test_supervision_only_after_k_and_card_enters_l2(self):
        a,m = self.agent(['Action 1: Finish[Honda]', SUPERVISED_DIAGNOSIS, 'Action 1: Search[Prius]',
                          'Action 2: Finish[Toyota]'])
        p = self.root / 'trial.json'
        exp = self.gather(a, path=p)
        self.assertEqual(exp.trial_rewards, (False, True))
        self.assertEqual(exp.trial_phases, ('autonomous','supervised'))
        self.assertFalse(exp.autonomous_solved)
        self.assertTrue(exp.supervised_repaired)
        self.assertNotIn('Reference answer:', m.prompts[0])
        self.assertIn('Reference answer: Toyota', m.prompts[1])
        self.assertEqual(exp.initial_skill_key, self.skill.key)
        success, failure, stats, notes = ED.build_histories([exp], lambda s: len(s)//4)
        self.assertIn('"phase": "supervised"', success)
        self.assertIn('Toyota', success)
        self.assertIsNone(failure)
        a2,m2 = self.agent([])
        self.assertEqual(self.gather(a2, path=p), exp)
        self.assertEqual(len(m2.prompts), 0)
    def test_structured_state_in_next_attempt_and_no_supervision_on_success(self):
        state = SUPERVISED_DIAGNOSIS
        a,m = self.agent(['Action 1: Finish[Honda]', state,
                          'Action 1: Search[Prius]', 'Action 2: Finish[Toyota]'])
        exp = self.gather(a, k=2)
        self.assertEqual(exp.trial_phases, ('autonomous','autonomous'))
        self.assertTrue(exp.autonomous_solved)
        self.assertIn('Honda', m.prompts[2])
        self.assertIn('Search Prius then', m.prompts[2])
        self.assertNotIn('Reference answer:', '\n'.join(m.prompts))
    def test_no_action_does_not_reach_environment(self):
        a,m = self.agent(['Thought 1: wait']*6)
        with patch.object(a.env, 'step', wraps=a.env.step) as step:
            exp = self.gather(a, supervised=False)
            self.assertEqual(step.call_count, 0)
        self.assertFalse(exp.reward)
        self.assertEqual(exp.l1_trials[0]['termination'], 'no_action_progress')
    def test_action_only_recovery_executes_instead_of_premature_failure(self):
        a,m = self.agent(['Thought 1: I will submit Toyota']*4 +
                        ['Action 1: Finish[Toyota]'])
        exp = self.gather(a, supervised=False)
        self.assertTrue(exp.reward)
        self.assertEqual(len(exp.l1_trials[0]['events']),5)
        self.assertEqual(exp.l1_trials[0]['events'][-1]['request_mode'],'action_only')
        self.assertIn('Action-only recovery',m.prompts[4])

    def test_supervision_sees_last_autonomous_failure_and_resume_keeps_it(self):
        a,m = self.agent(['Action 1: Finish[Honda]', 'bad json',
                         'Action 1: Finish[Ford]', SUPERVISED_DIAGNOSIS, 'Action 1: Finish[Toyota]'])
        p=self.root/'fresh-supervision.json'
        self.gather(a,k=2,path=p)
        data=json.loads(p.read_text())
        state=data['trials'][-1]['repair_state']
        self.assertIn('Finish[Ford]',str(state['failed_attempts']))
        self.assertEqual(state['latest_attempt']['trial'],2)
        self.assertIn('Ford',m.prompts[3])
        self.assertEqual(state['next_change']['actions'],['Finish[Toyota]'])
        a,m=self.agent([])
        self.assertTrue(self.gather(a,k=2,path=p).reward)
        self.assertFalse(m.prompts)

    def test_supervision_sees_last_no_action_termination(self):
        a,m=self.agent(['Thought 1: wait']*6+[SUPERVISED_DIAGNOSIS, 'Action 1: Finish[Toyota]'])
        exp=self.gather(a)
        self.assertEqual(exp.l1_trials[-1]['repair_state']['latest_attempt']['termination'],
                         'no_action_progress')
    def test_unknown_adapter_has_no_supervision(self):
        self.assertIsNone(Adapter().prepare_guidance(None, []))
    def test_interrupted_supervision_resumes_without_rediagnosing(self):
        a,m = self.agent(['Action 1: Finish[Honda]', SUPERVISED_DIAGNOSIS, RuntimeError('transport')])
        p = self.root / 'trial.json'
        with self.assertRaisesRegex(RuntimeError,'transport'):
            self.gather(a,path=p)
        a,m = self.agent(['Action 1: Finish[Toyota]'])
        exp = self.gather(a,path=p)
        self.assertTrue(exp.reward)
        self.assertEqual(exp.trial_phases, ('autonomous', 'supervised'))
        self.assertEqual(len(json.loads(p.read_text())['reflections']), 1)
        self.assertEqual(len(m.prompts),2)
        self.assertEqual(len(json.loads(p.read_text())['trials']),3)
    def test_unverified_quotes_not_promoted(self):
        raw = json.loads(SUPERVISED_DIAGNOSIS)
        raw['diagnosis']['kind']='missed_constraint_or_evidence'
        raw['evidence_refs']=['invented','t1:e1']
        raw['failed_attempts']=['imaginary']
        state=P.parse(json.dumps(raw),{'t1:e1':{'source':'observation','text':'Answer is INCORRECT'}})
        self.assertEqual(state['diagnosis']['kind'],'uncertain')
        self.assertNotIn('imaginary',str(state))

    def test_card_retains_core_over_budget(self):
        a,m=self.agent(['Action 1: Finish[Toyota]'])
        exp=self.gather(a)
        card=L.card(0,'task '*100,list(exp.l1_trials),{},None,'task-0',len,target=20,limit=40)
        self.assertTrue(card['over_budget'])
        self.assertEqual(card['task']['text'],'task '*100)
    def test_reconstruct_selected_skill_identity(self):
        spec=PL.ExperienceSpec(unit_id='x',benchmark='searchqa',task_id=0,family_id='family-p001',
            split='source',skill_key=self.skill.key,skill_body=self.skill.body,skill_description=self.skill.description,
            selected_skill_id=self.skill.skill_id)
        self.assertEqual(PL._reconstruct_skill(spec).key,self.skill.key)

if __name__ == '__main__':
    unittest.main()
