"""Regressions for false promotion, final coverage, factual evidence and isolation."""
import json
import copy
import unittest
from skillexpand.l1 import protocol as P, learning as L
from skillexpand.l1.adapters import AlfworldAdapter, SearchQAAdapter
from tests import test_l1_repair as fixtures


def trial(events, success=True, index=1):
    return dict(index=index, phase='autonomous', status='completed', success=success,
                termination='success' if success else 'step_budget', events=events)


def output(ref, kind='procedure', instruction='Use the observed method.'):
    return json.dumps({'claims':[dict(kind=kind,text=instruction,evidence_refs=[ref])]})


class LearningTests(unittest.TestCase):
    def test_explicit_empty_extraction_is_distinct_from_invalid_output(self):
        empty=L.finish(L.parse('{"claims":[]}',{},False))
        invalid=L.finish(L.parse('not json',{},False),L.parse('not json',{},False))
        self.assertEqual((empty['status'],empty['outcome']),('valid','empty_by_model'))
        self.assertEqual((invalid['status'],invalid['outcome']),('invalid','invalid_after_repair'))

    def test_incomplete_code_fence_is_invalid_not_an_exception(self):
        for raw in ('```', '```json'):
            self.assertEqual(L.parse(raw, {}, False)['status'], 'invalid')
            self.assertFalse(P.parse(raw, {})['valid'])

    def test_failed_action_inside_success_is_never_positive_support(self):
        # ALFWorld task 19: bad take was attempted; a different action solved it.
        t=trial([dict(ref='e1',action='take plate 1',observation='Nothing happens.'),
                 dict(ref='e2',action='take plate 1 from countertop 1',observation='You pick up the plate 1.')])
        refs=P.evidence('find plate',[t],adapter=AlfworldAdapter())
        self.assertEqual(L.parse(output('t1:e1'),refs,True)['status'],'invalid')
        self.assertEqual(L.parse(output('t1:e2'),refs,True)['status'],'valid')
        self.assertEqual(L.parse(output('t1:e1','constraint'),refs,False)['status'],'valid')

    def test_complete_procedure_is_not_lost_to_an_arbitrary_citation_limit(self):
        events=[dict(ref=f'e{i}',action=f'step {i}',observation=f'effect {i}') for i in range(7)]
        refs=P.evidence('q',[trial(events)],adapter=AlfworldAdapter())
        raw=json.loads(output('t1:e0'))
        raw['claims'][0]['evidence_refs']=list(refs)[1:]
        self.assertEqual(L.parse(json.dumps(raw),refs,True)['status'],'valid')

    def test_model_thoughts_and_task_goals_cannot_support_a_procedure(self):
        t=trial([dict(ref='e1',model_text='There is a plate in cabinet 1.'),
                 dict(ref='e2',action='examine cabinet 1',observation='It is empty.')])
        refs=P.evidence('find plate',[t])
        self.assertNotIn('t1:e1/model',refs)
        for ref in ('task','t1:e1/model','invented'):
            self.assertEqual(L.parse(output(ref),refs,True)['status'],'invalid')

    def test_no_success_cannot_support_procedure_but_can_support_constraint(self):
        refs=P.evidence('q',[trial([dict(ref='e1',action='Search[x]',observation='No result.')],False)])
        self.assertEqual(L.parse(output('t1:e1'),refs,False)['status'],'invalid')
        self.assertEqual(L.parse(output('t1:e1','constraint'),refs,False)['status'],'valid')

    def test_pure_searchqa_reference_submission_is_not_a_method(self):
        refs=P.evidence('q',[trial([dict(ref='e1',action='Finish[Toyota]',observation='Answer is CORRECT')])],adapter=SearchQAAdapter())
        self.assertEqual(L.parse(output('t1:e1'),refs,True)['status'],'invalid')

    def test_failed_trial_method_cannot_be_promoted_by_later_guided_success(self):
        failed=trial([dict(ref='e1',action='Search[x]',observation='No matching document.')],False)
        guided=trial([dict(ref='e1',action='Finish[Toyota]',observation='Answer is CORRECT')],True,2)
        guided['phase']='supervised'
        refs=P.evidence('q',[failed,guided],adapter=SearchQAAdapter())
        self.assertEqual(L.parse(output('t1:e1'),refs,True)['status'],'invalid')

    def test_procedure_cannot_mix_failed_and_successful_trials(self):
        failed=trial([dict(ref='e1',action='Search[wrong]',observation='No match.')],False)
        solved=trial([dict(ref='e1',action='Search[right]',observation='Found evidence.')],True,2)
        refs=P.evidence('q',[failed,solved],adapter=SearchQAAdapter())
        raw=json.loads(output('t2:e1'))
        raw['claims'][0]['evidence_refs']=['t1:e1','t2:e1']
        self.assertEqual(L.parse(json.dumps(raw),refs,True)['status'],'invalid')

    def test_one_rejected_claim_does_not_discard_a_supported_claim(self):
        refs=P.evidence('q',[trial([dict(ref='e1',action='Search[Prius]',
                                     observation='Toyota makes the Prius.')])],adapter=SearchQAAdapter())
        raw={'claims':[
            {'kind':'procedure','text':'Search the supplied document.',
             'evidence_refs':['t1:e1']},
            {'kind':'comparison','text':'Compare two attempts.',
             'evidence_refs':['t1:e1']},
        ]}
        parsed=L.parse(json.dumps(raw),refs,True)
        self.assertEqual(parsed['status'],'valid')
        self.assertEqual(len(parsed['claims']),1)
        self.assertEqual(parsed['rejected'][0]['index'],1)
        self.assertEqual(parsed['rejected'][0]['reason'],'comparison_requires_two_trials')
        self.assertEqual(L.repair_input({'evidence':refs},parsed)['open_slots'],1)

    def test_claim_limit_keeps_two_supported_claims_without_a_repair_slot(self):
        refs=P.evidence('q',[trial([dict(ref='e1',action='Search[Prius]',
                                     observation='Toyota makes the Prius.')])],adapter=SearchQAAdapter())
        rows=[{'kind':'constraint','text':f'Observation {i}.',
               'evidence_refs':['t1:e1']} for i in range(3)]
        parsed=L.parse(json.dumps({'claims':rows}),refs,True)
        self.assertEqual(len(parsed['claims']),2)
        self.assertEqual(parsed['rejected'][0]['reason'],'claim_limit_exceeded')
        self.assertEqual(L.finish(parsed)['outcome'],'partial')

    def test_no_placeholder_or_diagnostic_is_injected_as_skill(self):
        t=trial([dict(ref='e1',action='Search[x]',observation='document')])
        for status in ('valid','invalid','error'):
            c=L.card(0,'q',[t],dict(status=status,claims=[]),None,'x',len)
            self.assertEqual(c['claims'],[])
            self.assertEqual(L.skill_body(c),'')
            self.assertEqual(c['claim_status'],status)

    def test_whole_action_observation_pairs_survive_compaction(self):
        ts=[trial([dict(ref='e1',action='look',observation='old'*200)],False),
            trial([dict(ref='e2',action='open cabinet 1',observation='Empty.\nAdmissible actions: '+('go, '*1000))],False,2)]
        c=P.context('q',ts,{},None,len,limit=500,adapter=AlfworldAdapter())
        latest=c['evidence']['t2:e2']
        self.assertEqual(latest['text'],'Empty.')
        self.assertEqual(latest['action'],'open cabinet 1')
        self.assertNotIn('Admissible',json.dumps(c))
        self.assertGreater(c['omitted_evidence'],0)

    def test_alfworld_contract_is_present_in_all_three_prompts_only(self):
        alf=AlfworldAdapter();qa=SearchQAAdapter()
        for prompt in (alf.execution_instructions,alf.reflection_prompt(),alf.reflection_prompt(extraction=True)):
            self.assertIn('one carried object',prompt)
            self.assertIn('move <held object>',prompt)
            self.assertIn('Consecutive identical',prompt)
        for prompt in (qa.execution_instructions,qa.reflection_prompt(),qa.reflection_prompt(extraction=True)):
            self.assertNotIn('one carried object',prompt)
            self.assertNotIn('ALFWorld',prompt)

    def test_alfworld_demonstrations_use_actual_placement_command(self):
        from skillexpand.runtime.prompts import alfworld
        for text in alfworld.d.values():
            self.assertNotIn('> put ',text)
        for action in ('move plate 1 to table 1','examine drawer 1','close fridge 1','help'):
            _,kind,_=alfworld.LLM_PARSER(action,1,False)
            self.assertEqual(kind,'action')


class FinalSynthesisIntegration(unittest.TestCase):
    setUp=fixtures.L1RepairTests.setUp
    tearDown=fixtures.L1RepairTests.tearDown
    agent=fixtures.L1RepairTests.agent
    gather=fixtures.L1RepairTests.gather

    def test_audit_recomputes_records_and_rejects_corruption(self):
        from skillexpand.l1.audit import audit_checkpoint
        a,m=self.agent(['Action 1: Search[Prius]','Action 2: Finish[Toyota]'])
        m.synthesis=output('t1:e1')
        p=self.root/'audit.json';self.gather(a,path=p)
        data=json.loads(p.read_text())
        self.assertEqual(audit_checkpoint(data,SearchQAAdapter())['extraction_status'],'valid')
        mutations = [
            lambda d: d['experience'].update(reward=False),
            lambda d: d['trials'][0]['events'].append(d['trials'][0]['events'][0]),
            lambda d: d['synthesis']['input']['evidence']['t1:e1'].update(text='invented'),
            lambda d: d['synthesis'].update(raw='{}'),
            lambda d: d['synthesis']['initial_result'].update(claims=[]),
            lambda d: d['synthesis']['result'].update(outcome='invented'),
            lambda d: d['experience']['experience_card'].update(claims=[{'text':'invented'}]),
            lambda d: d['synthesis']['input'].update(omitted_evidence=99),
        ]
        for mutate in mutations:
            damaged=copy.deepcopy(data);mutate(damaged)
            with self.assertRaises(ValueError):
                audit_checkpoint(damaged,SearchQAAdapter())

    def test_extraction_failure_preserves_execution_and_is_auditable_on_resume(self):
        from skillexpand.l1.audit import audit_checkpoint
        a,m=self.agent(['Action 1: Finish[Toyota]']);m.synthesis='```json'
        p=self.root/'invalid.json';exp=self.gather(a,path=p)
        self.assertTrue(exp.reward)
        self.assertEqual(audit_checkpoint(json.loads(p.read_text()),SearchQAAdapter())['extraction_status'],'invalid')
        a,m=self.agent([])
        self.assertEqual(self.gather(a,path=p),exp)
        self.assertFalse(m.prompts)

    def test_extraction_transport_failure_resumes_only_synthesis(self):
        from skillexpand.l1.audit import audit_checkpoint
        a,m=self.agent(['Action 1: Finish[Toyota]']);m.synthesis=TimeoutError('transport')
        p=self.root/'transport.json'
        with self.assertRaises(TimeoutError):
            self.gather(a,path=p)
        interrupted=json.loads(p.read_text())
        self.assertNotIn('synthesis',interrupted)
        self.assertTrue(interrupted['trials'][0]['success'])
        a,m=self.agent([])
        exp=self.gather(a,path=p)
        self.assertTrue(exp.reward)
        self.assertEqual(len(m.prompts),1)
        data=json.loads(p.read_text())
        self.assertEqual(data['trials'],interrupted['trials'])
        audit_checkpoint(data,SearchQAAdapter())

    def test_partial_extraction_repairs_only_rejected_claim_and_audits_both_outputs(self):
        from skillexpand.l1.audit import audit_checkpoint
        a,m=self.agent(['Action 1: Search[Prius]','Action 2: Finish[Toyota]'])
        original=json.loads(output('t1:e1'))
        original['claims'].append({'kind':'comparison','text':'Compare two attempts.',
                                   'evidence_refs':['t1:e1']})
        m.synthesis=json.dumps(original)
        m.repair_synthesis=output('t1:e1','constraint','Use only observed search evidence.')
        path=self.root/'partial-extraction.json'
        exp=self.gather(a,path=path)
        data=json.loads(path.read_text())
        self.assertEqual(len(exp.experience_card['claims']),2)
        self.assertEqual(data['synthesis']['initial_result']['rejected'][0]['reason'],
                         'comparison_requires_two_trials')
        self.assertEqual(data['synthesis']['repair']['input']['open_slots'],1)
        self.assertEqual(data['synthesis']['result']['outcome'],'repaired')
        self.assertEqual(audit_checkpoint(data,SearchQAAdapter())['extraction_status'],'valid')
        self.assertEqual(len(m.prompts),4)
        self.assertIn('Repair only the rejected',m.prompts[-1])
        resumed=copy.deepcopy(data)
        del resumed['experience']
        del resumed['synthesis']['result']
        path.write_text(json.dumps(resumed))
        a,m=self.agent([])
        self.assertEqual(self.gather(a,path=path),exp)
        self.assertFalse(m.prompts)
        damaged=copy.deepcopy(data)
        damaged['synthesis']['repair']['raw']='{"claims":[]}'
        with self.assertRaises(ValueError):
            audit_checkpoint(damaged,SearchQAAdapter())

    def test_full_claim_slots_skip_unneeded_repair(self):
        from skillexpand.l1.audit import audit_checkpoint
        a,m=self.agent(['Action 1: Search[Prius]','Action 2: Finish[Toyota]'])
        m.synthesis=json.dumps({'claims':[
            {'kind':'constraint','text':f'Observed constraint {i}.',
             'evidence_refs':['t1:e1']} for i in range(3)]})
        path=self.root/'full-slots.json'
        exp=self.gather(a,path=path)
        synthesis=json.loads(path.read_text())['synthesis']
        self.assertEqual(len(exp.experience_card['claims']),2)
        self.assertEqual(synthesis['result']['outcome'],'partial')
        self.assertNotIn('repair',synthesis)
        self.assertEqual(len(m.prompts),3)
        audit_checkpoint(json.loads(path.read_text()),SearchQAAdapter())

    def test_malformed_extraction_can_be_repaired_to_an_explicit_empty_result(self):
        from skillexpand.l1.audit import audit_checkpoint
        a,m=self.agent(['Action 1: Finish[Toyota]'])
        m.synthesis='not json'
        m.repair_synthesis='{"claims":[]}'
        path=self.root/'repaired-empty.json'
        exp=self.gather(a,path=path)
        self.assertEqual(exp.experience_card['claim_status'],'valid')
        self.assertEqual(exp.experience_card['claims'],[])
        data=json.loads(path.read_text())
        self.assertEqual(data['synthesis']['result']['outcome'],'empty_after_repair')
        self.assertEqual(data['synthesis']['initial_result']['rejected'][0]['reason'],
                         'invalid_json')
        audit_checkpoint(data,SearchQAAdapter())

    def test_repair_transport_failure_resumes_without_repeating_extraction(self):
        from skillexpand.l1.audit import audit_checkpoint
        a,m=self.agent(['Action 1: Finish[Toyota]'])
        m.synthesis='not json'
        m.repair_synthesis=TimeoutError('repair transport')
        path=self.root/'repair-transport.json'
        with self.assertRaises(TimeoutError):
            self.gather(a,path=path)
        checkpoint=json.loads(path.read_text())
        self.assertIn('initial_result',checkpoint['synthesis'])
        self.assertNotIn('repair',checkpoint['synthesis'])
        self.assertNotIn('experience',checkpoint)
        a,m=self.agent([])
        m.repair_synthesis='{"claims":[]}'
        exp=self.gather(a,path=path)
        self.assertEqual(exp.experience_card['claim_status'],'valid')
        self.assertEqual(len(m.prompts),1)
        self.assertIn('Repair only the rejected',m.prompts[0])
        data=json.loads(path.read_text())
        self.assertEqual(data['trials'],checkpoint['trials'])
        self.assertEqual(data['synthesis']['result']['outcome'],'empty_after_repair')
        audit_checkpoint(data,SearchQAAdapter())

    def test_resume_after_saved_synthesis_does_not_repeat_execution_or_learning(self):
        a,m=self.agent(['Action 1: Search[Prius]','Action 2: Finish[Toyota]'])
        m.synthesis=output('t1:e1')
        p=self.root/'synthesized.json';exp=self.gather(a,path=p)
        data=json.loads(p.read_text());del data['experience']
        p.write_text(json.dumps(data))
        a,m=self.agent([])
        self.assertEqual(self.gather(a,path=p),exp)
        self.assertFalse(m.prompts)

    def test_usage_audit_rejects_duplicate_inflight_and_wrong_totals(self):
        from skillexpand.l1.audit import audit_usage
        p=self.root/'unit.json';usage=p.with_suffix('.usage.json')
        requests=usage.with_suffix('.requests.jsonl')
        tokens=dict(prompt_tokens=10,completion_tokens=2,total_tokens=12)
        totals=dict(started_requests=1,successful_requests=1,failed_requests=0,**tokens)
        rows=[dict(event='start',run_id='r'),
              dict(event='end',run_id='r',provider={'token_usage':tokens})]
        def write(values):
            requests.write_text('\n'.join(json.dumps(v) for v in values))
        usage.write_text(json.dumps(totals));write(rows)
        self.assertEqual(audit_usage(p)['requests'],1)
        data={'synthesis':{'input':{'evidence':{}},'raw':'actual output',
                           'result':{'status':'valid'}}}
        rows[0]['prompts']=[json.dumps(data['synthesis']['input'])]
        rows[1]['generations']=[[{'text':'actual output'}]]
        write(rows);self.assertEqual(audit_usage(p,data)['requests'],1)
        data['synthesis']['raw']='invented output'
        with self.assertRaises(ValueError):
            audit_usage(p,data)
        for corrupt in (rows+rows,rows[:1],rows[1:]):
            write(corrupt)
            with self.assertRaises(ValueError):
                audit_usage(p)
        write(rows);totals['total_tokens']=99;usage.write_text(json.dumps(totals))
        with self.assertRaises(ValueError):
            audit_usage(p)

    def test_usage_audit_verifies_initial_and_repair_responses(self):
        from skillexpand.l1.audit import audit_usage
        path=self.root/'two-requests.json'
        usage=path.with_suffix('.usage.json')
        requests=usage.with_suffix('.requests.jsonl')
        tokens=dict(prompt_tokens=3,completion_tokens=2,total_tokens=5)
        initial={'evidence':{'task':{'text':'q'}}}
        repair={'task':{'text':'q'},'rejected_claims':[{'reason':'invalid_json'}]}
        data={'synthesis':{'input':initial,'raw':'bad',
                           'repair':{'input':repair,'raw':'{"claims":[]}'}}}
        rows=[]
        for rid,payload,raw in (('initial',initial,'bad'),('repair',repair,'{"claims":[]}')):
            rows.extend([{'event':'start','run_id':rid,
                          'prompts':[json.dumps(payload,ensure_ascii=False)]},
                         {'event':'end','run_id':rid,'provider':{'token_usage':tokens},
                          'generations':[[{'text':raw}]]}])
        requests.write_text('\n'.join(json.dumps(row) for row in rows))
        usage.write_text(json.dumps(dict(started_requests=2,successful_requests=2,
            failed_requests=0,prompt_tokens=6,completion_tokens=4,total_tokens=10)))
        self.assertEqual(audit_usage(path,data)['requests'],2)
        data['synthesis']['repair']['raw']='invented'
        with self.assertRaisesRegex(ValueError,'extraction repair raw output'):
            audit_usage(path,data)

    def test_direct_success_gets_one_synthesis_and_resume_does_not_resample(self):
        a,m=self.agent(['Action 1: Search[Prius]','Action 2: Finish[Toyota]'])
        m.synthesis=output('t1:e1',instruction='Search Prius and answer from the maker in the returned document.')
        p=self.root/'direct.json';exp=self.gather(a,path=p)
        self.assertEqual(len(exp.experience_card['claims']),1)
        self.assertEqual(exp.experience_card['schema_version'],5)
        data=json.loads(p.read_text());self.assertEqual(data['reflections'],[])
        self.assertEqual(data['synthesis']['result']['status'],'valid')
        self.assertEqual(data['synthesis']['result']['outcome'],'accepted')
        self.assertEqual(len(m.prompts),3)
        a,m=self.agent([]);self.assertEqual(self.gather(a,path=p),exp);self.assertFalse(m.prompts)

    def test_final_failure_uses_last_feedback_and_not_old_format_stall(self):
        from tests.test_l1_repair import SUPERVISED_DIAGNOSIS
        a,m=self.agent(['Thought 1: wait']*6+[SUPERVISED_DIAGNOSIS,'Action 1: Finish[Honda]'])
        m.synthesis=output('t2:e1','constraint','Do not submit Honda for this clue; it was rejected.')
        exp=self.gather(a,k=2,supervised=False)
        self.assertFalse(exp.reward)
        self.assertIn('Honda',L.skill_body(exp.experience_card))
        self.assertNotIn('formatting budget',L.skill_body(exp.experience_card))
        self.assertIn('Finish[Honda]',m.prompts[-1])

    def test_parse_failure_is_audited_and_not_a_negative_learning_claim(self):
        a,m=self.agent(['Action 1: Finish[Toyota]']);m.synthesis='{"state": {}}'
        p=self.root/'invalid.json';exp=self.gather(a,path=p)
        self.assertTrue(exp.reward);self.assertEqual(exp.experience_card['claim_status'],'invalid')
        self.assertEqual(exp.experience_card['claims'],[])
        self.assertEqual(exp.experience_card['evidence'][-1]['action'],'Finish[Toyota]')
        synthesis=json.loads(p.read_text())['synthesis']
        self.assertEqual(synthesis['raw'],m.synthesis)
        self.assertEqual(synthesis['result']['outcome'],'invalid_after_repair')
