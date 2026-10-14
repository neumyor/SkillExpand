import json
from pathlib import Path
import pytest
import state
from driver import select
from generation import inputs


def put(p,x):
    p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(x))

def test_exact_slot_budget_and_reservation_cannot_repeat(tmp_path,monkeypatch):
    monkeypatch.setattr(state,'ROOT',tmp_path)
    calls=0
    for task in state.TASKS:
        root=tmp_path/'tasks'/task
        for i in range(1,6):
            slot=root/'baseline'/f'{i:02d}'
            assert state.reserve(slot,'executor');calls+=1
            assert not state.reserve(slot,'executor')
        for arm in state.ARMS:
            for candidate in (1,2):
                c=root/'arms'/arm/f'candidate-{candidate:03d}'
                assert state.reserve(c,'generation')
                assert not state.reserve(c,'generation')
                for i in range(1,4):
                    assert state.reserve(c/'dev'/f'{i:02d}','executor');calls+=1
            for i in range(1,6):
                assert state.reserve(root/'arms'/arm/'final'/f'{i:02d}','executor');calls+=1
    assert calls==81
    with pytest.raises(ValueError):state.reserve(tmp_path/'tasks'/state.TASKS[0]/'baseline/06','executor')
    with pytest.raises(ValueError):state.reserve(tmp_path/'tasks'/state.TASKS[0]/'arms/A/candidate-003','generation')


def test_selection_tie_failures_and_no_usable_package():
    rows=[{'number':1,'generated':False,'successes':0},{'number':2,'generated':False,'successes':0}]
    assert select(rows) is None
    rows[1]['generated']=True
    assert select(rows)==2
    rows[0]['generated']=True
    assert select(rows)==1
    rows[1]['successes']=2
    assert select(rows)==2


def test_candidate_two_feedback_is_exact_same_arm_dev(tmp_path,monkeypatch):
    import generation
    monkeypatch.setattr(state,'ROOT',tmp_path)
    monkeypatch.setattr(generation,'ROOT',tmp_path)
    task=state.TASKS[0]
    root=tmp_path/'tasks'/task
    put(root/'frozen/seed.json',{'key':'seed'})
    put(root/'frozen/evidence-A.json',{'allowed':'history'})
    put(root/'arms/A/candidate-001/skill.json',{'key':'autonomous-A'})
    for i in range(1,4):put(root/'arms/A/candidate-001/dev'/f'{i:02d}'/'feedback.json',{'dev':i})
    put(root/'arms/A/final/01/feedback.json',{'forbidden':'final'})
    put(root/'arms/B/candidate-001/dev/01/feedback.json',{'forbidden':'other-arm'})
    base,evidence,feedback,paths=inputs(task,'A',2)
    assert base=={'key':'autonomous-A'}
    assert feedback==[{'dev':i} for i in range(1,4)]
    assert all('/A/candidate-001/dev/' in str(p) for p in paths)


def test_interrupted_slot_never_invokes_executor(tmp_path,monkeypatch):
    import execution
    monkeypatch.setattr(state,'ROOT',tmp_path)
    monkeypatch.setattr(execution,'ROOT',tmp_path)
    monkeypatch.setattr(execution,'verify_freeze',lambda:None)
    slot=tmp_path/'tasks'/state.TASKS[0]/'baseline/01'
    assert state.reserve(slot,'executor')
    result=execution.run(slot,None)
    assert result['error_type']=='InterruptedReservedSlot'
    assert result['status']=='unresolved' and result['no_retry']


def test_generation_failure_is_not_retried(tmp_path,monkeypatch):
    import generation
    monkeypatch.setattr(state,'ROOT',tmp_path)
    monkeypatch.setattr(generation,'ROOT',tmp_path)
    monkeypatch.setattr(generation,'verify_freeze',lambda:None)
    task=state.TASKS[0]
    slot=tmp_path/'tasks'/task/'arms/A/candidate-001'
    assert state.reserve(slot,'generation')
    assert not generation.generate(task,'A',1)
    assert state.read(slot/'generation_status.json')['no_retry']


def test_generation_initialization_failure_retained_and_next_slot_available(tmp_path,monkeypatch):
    import generation
    from input_filter import sanitize_seed
    monkeypatch.setattr(state,'ROOT',tmp_path);monkeypatch.setattr(generation,'ROOT',tmp_path)
    monkeypatch.setattr(generation,'verify_freeze',lambda:None)
    monkeypatch.setenv('TB21_METHOD_API_KEY','test-key')
    def broken():raise RuntimeError('initialization outage')
    monkeypatch.setattr(generation,'relay_from_env',broken)
    seed=sanitize_seed({'skill_id':'id','family_id':'f','version':0,'name':'Name','description':'Route'})
    task=state.TASKS[0];root=tmp_path/'tasks'/task
    put(root/'frozen/seed.json',seed);put(root/'frozen/evidence-A.json',{})
    put(tmp_path/'frozen/generation_settings.json',{'model':'method','stream':True})
    assert not generation.generate(task,'A',1)
    slot=root/'arms/A/candidate-001'
    assert state.read(slot/'generation_status.json')['error_type']=='RuntimeError'
    assert (slot/'request.json').exists()
    assert not generation.generate(task,'A',1)
    assert not generation.generate(task,'A',2)
    assert (root/'arms/A/candidate-002/generation_status.json').exists()


def test_duplicate_invalidates_first_record_and_blocks_new_calls(tmp_path,monkeypatch):
    import execution
    monkeypatch.setattr(state,'ROOT',tmp_path);monkeypatch.setattr(execution,'ROOT',tmp_path)
    put(tmp_path/'frozen/historical_sandbox_ids.json',[])
    one=tmp_path/'tasks'/state.TASKS[0]/'baseline/01'
    two=tmp_path/'tasks'/state.TASKS[1]/'baseline/01'
    r1={'status':'valid','reward':1,'sandbox_id':'same'}
    execution.register_identity(one,r1);put(one/'record.json',r1)
    r2={'status':'valid','reward':1,'sandbox_id':'same'}
    execution.register_identity(two,r2)
    assert r2['status']=='unresolved'
    assert state.read(one/'record.json')['status']=='unresolved'
    with pytest.raises(ValueError,match='identity violation'):state.verify_freeze()
