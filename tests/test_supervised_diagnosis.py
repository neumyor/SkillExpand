"""Unified V4 reflection, guidance isolation, resume and L2 boundary checks."""
import json
import unittest
from unittest.mock import patch
from omegaconf import OmegaConf
from tests import test_l1_repair as fixtures
from skillexpand.l1 import protocol as P
from skillexpand.l1.adapters import SearchQAAdapter
from skillexpand.l1.adapters import AlfworldAdapter
from skillexpand.l1.adapters import resolve
from skillexpand.l1.adapters import PROMPT_FIELDS
from skillexpand.l2 import editor as ED

D = fixtures.SUPERVISED_DIAGNOSIS

class SupervisedDiagnosisTests(unittest.TestCase):
    setUp = fixtures.L1RepairTests.setUp
    tearDown = fixtures.L1RepairTests.tearDown
    agent = fixtures.L1RepairTests.agent
    gather = fixtures.L1RepairTests.gather

    def test_explicit_diagnosis_precedes_action_and_has_fresh_evidence(self):
        a,m=self.agent(['Action 1: Finish[Honda]', D, 'Action 1: Finish[Toyota]'])
        p=self.root/'diagnosis.json'
        exp=self.gather(a,path=p)
        data=json.loads(p.read_text())
        self.assertNotIn('Reference answer:',m.prompts[0])
        self.assertIn('guidance_delta',m.prompts[1])
        self.assertIn('Finish[Honda]',m.prompts[1])
        self.assertIn('Current task repair state',m.prompts[2])
        self.assertEqual(len(data['reflections']),1)
        self.assertEqual(data['state']['latest_attempt']['trial'],2)
        self.assertTrue(data['state']['latest_attempt']['success'])
        self.assertEqual(exp.experience_card['claims'],[])
        self.assertEqual(len(m.prompts),4)
        self.assertEqual(data['synthesis']['result']['status'],'valid')
        a,m=self.agent([])
        self.assertEqual(self.gather(a,path=p),exp)
        self.assertFalse(m.prompts)

    def test_reflection_transport_error_does_not_consume_supervised_trial(self):
        a,m=self.agent(['Action 1: Finish[Honda]',RuntimeError('transport')])
        p=self.root/'retry.json'
        with self.assertRaisesRegex(RuntimeError,'transport'):
            self.gather(a,path=p)
        data=json.loads(p.read_text())
        self.assertEqual(len(data['trials']),1)
        self.assertEqual(data['errors'][0]['stage'],'reflection')
        a,m=self.agent([D,'Action 1: Finish[Toyota]'])
        self.assertTrue(self.gather(a,path=p).reward)
        self.assertEqual(len(json.loads(p.read_text())['trials']),2)

    def test_persisted_reflection_reused_after_pretrial_crash(self):
        import skillexpand.l1.runner as runner
        original=runner.save
        p=self.root/'crash.json'
        def crash(path,data):
            original(path,data)
            if data['reflections'] and len(data['trials'])==1:
                raise RuntimeError('crash after diagnosis persisted')
        a,m=self.agent(['Action 1: Finish[Honda]',D])
        with patch.object(runner,'save',crash):
            with self.assertRaisesRegex(RuntimeError,'persisted'):
                self.gather(a,path=p)
        a,m=self.agent(['Action 1: Finish[Toyota]'])
        self.assertTrue(self.gather(a,path=p).reward)
        self.assertEqual(len(m.prompts),2)

    def test_mismatch_extracts_once_from_actual_actions_and_resumes(self):
        planned=json.loads(D)
        planned['next_change']['actions']=['Search[Mars]']
        a,m=self.agent(['Action 1: Finish[Honda]',json.dumps(planned),
                       'Action 1: Finish[Toyota]',D])
        p=self.root/'extract.json'
        exp=self.gather(a,path=p)
        data=json.loads(p.read_text())
        self.assertEqual([r['purpose'] for r in data['reflections']],['repair'])
        self.assertIn('synthesis',data)
        self.assertIn("The task's execution is finished.",m.prompts[-1])
        self.assertEqual(exp.experience_card['execution']['trials'][-1]['success'],True)
        self.assertNotIn('Search[Mars]',str(exp.experience_card))
        self.assertEqual(exp.experience_card['claims'],[])
        a,m=self.agent([])
        self.assertEqual(self.gather(a,path=p),exp)
        self.assertFalse(m.prompts)

    def test_invalid_extraction_falls_back_without_losing_execution(self):
        a,m=self.agent(['Action 1: Finish[Honda]','not json','Action 1: Finish[Toyota]'])
        m.synthesis='not json'
        exp=self.gather(a)
        self.assertTrue(exp.reward)
        self.assertEqual(exp.experience_card['claim_status'],'invalid')
        self.assertEqual(exp.experience_card['claims'],[])
        self.assertEqual(len(m.prompts),5)

    def test_final_failure_is_synthesized_from_final_evidence(self):
        a,m=self.agent(['Action 1: Finish[Honda]',D,'Action 1: Finish[Ford]'])
        p=self.root/'failure.json'
        exp=self.gather(a,path=p)
        data=json.loads(p.read_text())
        self.assertFalse(exp.reward)
        self.assertEqual(data['state']['latest_attempt']['feedback']['submitted_answers'],['Ford'])
        self.assertEqual(len(data['reflections']),1)
        self.assertEqual(len(m.prompts),4)

    def test_disabled_guidance_never_queries_provider(self):
        a,m=self.agent(['Action 1: Finish[Honda]'])
        with patch.object(SearchQAAdapter,'prepare_guidance',side_effect=AssertionError('GT requested')):
            exp=self.gather(a,supervised=False)
        self.assertFalse(exp.reward)
        self.assertEqual(len(m.prompts),2)

    def test_missing_guidance_has_no_guided_call(self):
        a,m=self.agent(['Action 1: Finish[Honda]'])
        with patch.object(SearchQAAdapter,'prepare_guidance',return_value=None) as provider:
            exp=self.gather(a)
        provider.assert_called_once()
        self.assertFalse(exp.reward)
        self.assertEqual(len(m.prompts),2)
        self.assertIsNone(AlfworldAdapter().prepare_guidance(None,[]))

    def test_benchmark_owned_feedback_and_non_answer_guidance(self):
        a,m=self.agent(['Action 1: Finish[Honda]',D,'Action 1: Finish[Toyota]'])
        payload=['teacher','inspect the maker in D1']
        with patch.object(SearchQAAdapter,'prepare_guidance',return_value=payload),\
             patch.object(SearchQAAdapter,'render_guidance',side_effect=lambda g: g[1]),\
             patch.object(SearchQAAdapter,'build_feedback',return_value=['custom',{'accepted':False}]):
            exp=self.gather(a)
        self.assertTrue(exp.reward)
        self.assertIn('inspect the maker in D1',m.prompts[1])
        self.assertNotIn('Reference answer:', '\n'.join(m.prompts))
        self.assertEqual(exp.l1_trials[-1]['repair_state']['latest_attempt']['feedback'],
                         ['custom',{'accepted':False}])

    def test_bad_citations_cannot_establish_oversight_or_bad_reference(self):
        for kind in ('missed_constraint_or_evidence','suspected_reference_or_scoring_issue'):
            value=json.loads(D)
            value['diagnosis']['kind']=kind
            value['evidence_refs']=['t1:e1','guidance','invented']
            result=P.parse(json.dumps(value),{
                't1:e1':{'source':'observation','text':'Answer is INCORRECT'},
                'guidance':{'source':'guidance','text':'Toyota'}},guided=True)
            self.assertEqual(result['diagnosis']['kind'],'uncertain')
            self.assertNotIn('invented',result['evidence_refs'])
            value['evidence_refs']=['t1:e1']
            result=P.parse(json.dumps(value),{'t1:e1':{
                'source':'observation','phase':'autonomous','text':'Toyota makes Prius'}},guided=True)
            self.assertEqual(result['diagnosis']['kind'],kind)

    def test_guided_observation_cannot_establish_prior_oversight(self):
        value=json.loads(D)
        value['diagnosis']['kind']='missed_constraint_or_evidence'
        value['evidence_refs']=['t2:e1']
        result=P.parse(json.dumps(value),{'t2:e1':{'source':'observation',
            'phase':'supervised','text':'Toyota makes Prius'}},guided=True)
        self.assertEqual(result['diagnosis']['kind'],'uncertain')

    def test_failed_trial_search_is_retained_but_not_promoted_by_later_success(self):
        value=json.loads(D)
        value['diagnosis']['kind']='missed_constraint_or_evidence'
        value['evidence_refs']=['t1:e1']
        a,m=self.agent(['Action 1: Search[Prius]', 'Action 2: Finish[Honda]',
                       json.dumps(value),'Action 1: Finish[Toyota]'])
        m.synthesis=json.dumps({'claims':[{'kind':'procedure','text':'Search the supplied Prius document before answering.', 'evidence_refs':['t1:e1']}]})
        exp=self.gather(a,k=2,supervised=False)
        self.assertEqual(exp.experience_card['claims'],[])
        self.assertEqual(exp.experience_card['claim_status'],'invalid')
        self.assertEqual(exp.experience_card['execution']['trials'][-1]['success'],True)
        self.assertNotIn('guidance',exp.experience_card)
        self.assertNotIn('guidance_delta',exp.l1_trials[-1]['repair_state'])
        self.assertEqual(exp.experience_card['evidence'][0]['phase'],'autonomous')
        self.assertEqual(len(m.prompts),6)

    def test_all_batch_cards_survive_low_render_budget(self):
        from dataclasses import replace
        a,m=self.agent(['Action 1: Finish[Toyota]'])
        exp=self.gather(a)
        items=[replace(exp,experience_id=f'x{i}',task_id=i) for i in range(16)]
        success,_,stats,_=ED.build_histories(items,len,success_budget=1)
        self.assertEqual(stats['successes'],16)
        self.assertEqual(success.count('TASK-SPECIFIC EXPERIENCE CARD'),16)

    def test_l2_allowlist_and_neutral_full_prompt(self):
        a,m=self.agent(['Action 1: Finish[Honda]',D,'Action 1: Finish[Toyota]'])
        exp=self.gather(a)
        exp.experience_card['private_analysis']='DO_NOT_SEND_ANALYSIS'
        exp.experience_card['guidance']={'payload':'DO_NOT_SEND_REFERENCE_BLOB'}
        success,failed,stats,notes=ED.build_histories([exp],len)
        for sentinel in ('DO_NOT_SEND_ANALYSIS','DO_NOT_SEND_REFERENCE_BLOB',
                         'REFLECTION-RECOVERED CASES'):
            self.assertNotIn(sentinel,success)
        self.assertIn('"phase": "supervised"',success)
        self.assertIn('No card is a mandatory repair target',notes)
        self.assertIsNone(failed)
        self.assertEqual(stats['successes'],1)
        editor=ED.SkillEditor(a)
        prompt,_,_=editor.build_prompt(self.skill,self.skill.body,[exp])
        text='\n'.join(x.content for x in prompt)
        self.assertIn('TASK EVIDENCE',text)
        self.assertIn('first Finish[answer] ends that attempt',text)
        self.assertIn('Do not propose or write rules',text)
        self.assertNotIn('REFLECTION-RECOVERED CASES',text)
        with patch.object(a,'llm',return_value='{"hypotheses":[]}') as call:
            editor.plan(self.skill,[exp],1)
        self.assertIn('first Finish[answer] ends that attempt',call.call_args.args[0][0].content)

    def test_all_prompt_overrides_and_signature_includes_file_content(self):
        for field in PROMPT_FIELDS:
            cfg=OmegaConf.create({'benchmark':{'name':'searchqa','l1':{field:'Custom '+field}}})
            self.assertEqual(getattr(resolve(cfg),field),'Custom '+field)
        file=self.root/'prompt.txt'
        file.write_text('Custom SearchQA execution')
        self.cfg.benchmark.l1.execution_instructions_file=str(file)
        self.cfg.benchmark.l1.selector_instructions='Custom selector SKILL: / WHY:'
        a,m=self.agent(['Action 1: Finish[Toyota]'])
        from skillexpand.evaluation.selector import SkillSelector
        self.assertIn('Custom selector',SkillSelector(a,resolve(self.cfg)).build_prompt('q',[self.skill])[0].content)
        p=self.root/'signature.json'
        self.gather(a,path=p)
        self.assertIn('Custom SearchQA execution',m.prompts[0])
        file.write_text('Changed execution')
        a,m=self.agent([])
        with self.assertRaisesRegex(ValueError,'mismatch'):
            self.gather(a,path=p)
        self.cfg.benchmark.l1.execution_instructions='conflict'
        with self.assertRaisesRegex(ValueError,'not both'):
            resolve(self.cfg)

    def test_context_keeps_whole_recent_observation(self):
        trials=[dict(index=1,events=[{'ref':'e1','observation':'old'*100}]),
                dict(index=2,events=[{'ref':'e1','observation':'new evidence with annotation',
                                     'model_text':'thought'*100}])]
        payload=P.context('q',trials,{},None,len,limit=200)
        self.assertEqual(payload['evidence']['t2:e1']['text'],'new evidence with annotation')
        self.assertNotIn('t1:e1',payload['evidence'])
        self.assertNotIn('t2:e1/model',payload['evidence'])

if __name__ == '__main__':
    unittest.main()
