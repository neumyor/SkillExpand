"""Single-shot trial wrapper around the already validated execution path."""
import fcntl
import os
from pathlib import Path
from state import ROOT,read,write,verify_freeze,reserve
from input_filter import project_tool_results


def safe_metrics(directory,record,skill):
    if not record.get('result_path'):return
    result_path=Path(record['result_path'])
    trace_path=result_path.parent/'agent/trajectory.json'
    if not trace_path.exists():return
    trace=read(trace_path)
    steps=trace.get('steps',[])
    record.update(metrics=trace.get('final_metrics'),rounds=len(steps),
                  tool_calls=sum(len(s.get('tool_calls') or []) for s in steps),
                  response_model=trace.get('agent',{}).get('model_name'))
    snapshot=result_path.parent/'snapshot.json'
    if snapshot.exists():record['sandbox_id']=read(snapshot).get('source_sandbox_id')
    evidence=[]
    resources=skill.resources if skill.package_mode else {}
    for idx,s in enumerate(steps):
        for c in s.get('tool_calls') or []:
            args=__import__('json').dumps(c.get('arguments',{}),ensure_ascii=False)
            for path in resources:
                if path in args:
                    evidence.append({'step_index':idx,'resource':path,'tool':c.get('function_name'),
                                     'command_arguments':c.get('arguments'),
                                     'observation_present':bool(s.get('observation')),
                                     'classification':'resource_path_referenced_not_proof_of_execution'})
    record['resource_usage_evidence']=evidence
    record['safe_tool_observations']=project_tool_results(trace)
    record['provider_request_records']=sum(1 for _ in (result_path.parent/'agent/raw_model_requests.jsonl').open()) if (result_path.parent/'agent/raw_model_requests.jsonl').exists() else None
    record['provider_response_records']=sum(1 for _ in (result_path.parent/'agent/raw_model_responses.jsonl').open()) if (result_path.parent/'agent/raw_model_responses.jsonl').exists() else None
    if record['status']=='valid':
        activation=read(result_path.parent/'agent/skill_activation.json')
        mounted=Path(activation['source_path'])
        from skillexpand.skill_package import entrypoint_content
        if mounted.resolve()!=(directory/'skills'/result_path.parent.name.split('__')[0]/'SKILL.md').resolve():
            raise ValueError('Mounted package path differs')
        if mounted.read_text()!=entrypoint_content(skill,package=skill.package_mode):raise ValueError('Mounted entrypoint differs')
        if skill.package_mode:
            for p,v in resources.items():
                if (mounted.parent/p).read_text()!=v:raise ValueError('Mounted resource differs')


def register_identity(directory,record):
    with (ROOT/'.identities.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        p=ROOT/'identity_ledger.json'
        ledger=read(p) if p.exists() else {}
        identity=record.get('sandbox_id')
        excluded=read(ROOT/'frozen/historical_sandbox_ids.json')
        prior=ledger.get(identity) if identity else None
        if not identity or identity in excluded or prior and prior!=str(directory.relative_to(ROOT)):
            record.update(status='unresolved',identity_valid=False,identity_error='missing_historical_or_duplicate_id')
            if identity and (identity in excluded or prior and prior!=str(directory.relative_to(ROOT))):
                if prior:
                    previous=ROOT/prior/'record.json'
                    if previous.exists():
                        old=read(previous);old.update(status='unresolved',identity_valid=False,identity_error='duplicate_id')
                        write(previous,old)
                write(ROOT/'integrity_abort.json',{'reason':'historical_or_duplicate_sandbox_identity',
                       'identity':identity,'prior_slot':prior,'new_slot':str(directory.relative_to(ROOT)),
                       'action':'block all further generation and executor calls; no replacement'})
        else:
            ledger[identity]=str(directory.relative_to(ROOT));write(p,ledger)
            record['identity_valid']=True


def run(directory,skill,development=False):
    directory=Path(directory)
    verify_freeze()
    if not reserve(directory,'executor'):
        if not (directory/'record.json').exists():
            write(directory/'record.json',{'status':'unresolved','error_type':'InterruptedReservedSlot',
                                           'no_retry':True,'identity_valid':False})
        record=read(directory/'record.json')
    else:
        import evaluator
        record=evaluator.run_trial(directory,skill)
        try:safe_metrics(directory,record,skill)
        except Exception as exc:
            record.update(status='unresolved',artifact_error_type=type(exc).__name__)
        register_identity(directory,record)
        record['no_retry']=True
        write(directory/'record.json',record)
    if development:
        trace={'steps':[]}
        if record.get('trajectory_path') and Path(record['trajectory_path']).exists():trace=read(Path(record['trajectory_path']))
        payload={'status':record['status'],'reward':record.get('reward') if record['status']=='valid' else None,
                 'tool_observations':project_tool_results(trace),'projection':'fixed-tool-results-8192-deny-evaluation-v1'}
        write(directory/'feedback.json',payload)
    verify_freeze()
    return record
