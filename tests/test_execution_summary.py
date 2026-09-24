import unittest,tempfile,json
from pathlib import Path
from skillexpand.l1 import learning as L
from unittest.mock import patch
from types import SimpleNamespace
from skillexpand.l1 import protocol as P
from skillexpand.runtime import parallel as PL
from skillexpand.runtime import agent_factory as F
from tests.test_l1_repair import Model

class ExecutionSummaryTests(unittest.TestCase):
    def trial(self,phase='autonomous',success=True,events=None):
        return dict(index=1,status='completed',phase=phase,success=success,termination='success' if success else 'step_budget',events=events or [])
    def test_direct_success_retains_lamp_execution_without_inventing_lesson(self):
        events=[{'ref':'e1','action':'take alarmclock 2 from desk 1','observation':'You pick up the alarmclock.'},
                {'ref':'e2','action':'use desklamp 1','observation':'Task is SOLVED.'}]
        trial=self.trial(events=events)
        card=L.card(0,'look at alarmclock under desklamp',[trial],{},None,'task-0',len,
                    evidence=P.evidence('look at alarmclock under desklamp',[trial]))
        self.assertEqual(card['claims'],[])
        self.assertEqual(card['evidence'][-1]['action'],'use desklamp 1')
        self.assertEqual(P.projection(card)['execution'],card['execution'])
    def test_all_outcomes_preserve_phase_and_only_executed_actions(self):
        for phase,success in [('autonomous',False),('supervised',False),('supervised',True)]:
            t=self.trial(phase,success,[{'ref':'e1','model_text':'I should use lamp'},
                {'ref':'e2','action':'Finish[x]','observation':'Answer is CORRECT' if success else 'Answer is INCORRECT'},
                {'ref':'e3','action':'Search[x]','blocked_action':'Search[x]'}])
            c=L.card(0,'q',[t],{},'reference','task-0',len)
            self.assertEqual(c['execution']['trials'][0]['phase'],phase)
            self.assertEqual(len(c['evidence']),0)
            self.assertEqual(c['execution']['success'],success)
    def test_summary_is_bounded_and_omission_explicit(self):
        events=[dict(ref=f'e{i}',action=f'go to cabinet {i}',observation='x'*300) for i in range(20)]
        events.append(dict(ref='e20',action='use lamp 1',observation='Task is SOLVED.'))
        result=P.execution_summary(self.trial(events=events))
        self.assertEqual(len(result['events']),6);self.assertEqual(result['omitted_actions'],15)
        self.assertEqual(result['operation_counts'],{'go':20,'use':1})
        self.assertTrue(result['events'][0]['observation_truncated'])
        self.assertEqual(result['events'][-1]['ref'],'t1:e20')
    def test_no_action_failure_still_has_summary(self):
        c=L.card(0,'q',[self.trial(success=False)],{},None,'task-0',len)
        self.assertEqual(c['evidence'],[])
    def test_searchqa_threads_keep_agents_and_checkpoints_isolated(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);tasks=root/'tasks.json'
            tasks.write_text(json.dumps([{'question':f'Find maker {i}','answers':['Toyota'],'context':'Toyota makes this.'} for i in range(16)]))
            cfg=F.load_config('searchqa');cfg.benchmark.task_file=str(tasks)
            specs=[PL.ExperienceSpec(unit_id=f't{i}',benchmark='searchqa',task_id=i,family_id='unassigned',split='source',skill_aware=False,max_trials=1,supervised_repair=False,l1_checkpoint_path=str(root/f'{i}.json')) for i in range(16)]
            with patch.object(PL,'_config',return_value=cfg),patch.object(F,'LLM_CLS',side_effect=lambda **kw:Model(['Action 1: Finish[Toyota]'])):
                results=PL.run_generic(specs,PL.execute_experience,workers=16)
            self.assertEqual(len(results),16)
            for r in results:
                self.assertTrue(r['ok'],r)
                exp=r['experience'];self.assertIn(str(r['task_id']),exp['task'])
                self.assertTrue(exp['reward'])
                self.assertEqual(len(json.loads((root/f"{r['task_id']}.json").read_text())['trials']),1)
                self.assertEqual(exp['experience_card']['execution']['trials'][0]['success'],True)
if __name__=='__main__':unittest.main()
