"""Report the fixed protocol from persisted artifacts without running any models."""
import json
from collections import Counter
from pathlib import Path
from state import ROOT,TASKS,ARMS,read,write


def paths(p):
    if isinstance(p,str):
        remote='/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/three-task-input-ablation-20261014'
        return Path(str(ROOT)+p[len(remote):]) if p.startswith(remote) else Path(p)
    return Path(p)


def totals(rows):
    known=[];unknown=0
    for row in rows:
        usage=row.get('metrics') or row.get('usage') or {}
        value=next((usage[k] for k in ('total_tokens','total_token_count') if isinstance(usage.get(k),(int,float))),None)
        if value is None:
            inp=next((usage[k] for k in ('prompt_tokens','input_tokens','total_prompt_tokens') if isinstance(usage.get(k),(int,float))),None)
            out=next((usage[k] for k in ('completion_tokens','output_tokens','total_completion_tokens') if isinstance(usage.get(k),(int,float))),None)
            if inp is not None and out is not None:value=inp+out
        if value is None:unknown+=1
        else:known.append(value)
    return {'observed_total':sum(known),'known_records':len(known),'unknown_records':unknown,'complete':unknown==0}


def describe(records):
    valid=[r for r in records if r.get('status')=='valid' and r.get('identity_valid')]
    return {'attempts':len(records),'valid':len(valid),'successes':sum(r.get('reward')==1 for r in valid),
            'rewards':[r.get('reward') if r in valid else None for r in records],
            'durations_seconds':[r.get('duration_seconds') for r in records],
            'rounds':[r.get('rounds') for r in records],
            'tool_calls':[r.get('tool_calls') for r in records],
            'usage':totals(records),'unresolved':len(records)-len(valid)}


def build():
    trials=[];generations=[];results=[]
    for task in TASKS:
        root=ROOT/'tasks'/task
        baseline=[read(p) for p in sorted((root/'baseline').glob('*/record.json'))]
        results.append({'task':task,'phase':'sanitized-original','summary':describe(baseline)})
        for arm in ARMS:
            folder=root/'arms'/arm
            for number in (1,2):
                cand=folder/f'candidate-{number:03d}'
                if (cand/'generation_status.json').exists():
                    row={'task':task,'arm':arm,'candidate':number,**read(cand/'generation_status.json')}
                    if (cand/'generation_metrics.json').exists():row.update(read(cand/'generation_metrics.json'))
                    generations.append(row)
                dev=[read(p) for p in sorted((cand/'dev').glob('*/record.json'))]
                results.append({'task':task,'phase':f'{arm}-candidate-{number}-dev','summary':describe(dev)})
            final=[read(p) for p in sorted((folder/'final').glob('*/record.json'))]
            results.append({'task':task,'phase':f'{arm}-final','selection':read(folder/'selection.json') if (folder/'selection.json').exists() else None,'summary':describe(final)})
        for p in sorted(root.rglob('record.json')):
            if 'frozen' in p.relative_to(root).parts:continue
            row=read(p);trials.append({'path':str(p.relative_to(ROOT)),**row})
    executor_reserved=sum(read(p)['kind']=='executor' for p in (ROOT/'tasks').rglob('reserved.json'))
    generation_reserved=sum(read(p)['kind']=='generation' for p in (ROOT/'tasks').rglob('reserved.json'))
    stage_complete=all((ROOT/'tasks'/t/'baseline/summary.json').exists() and all((ROOT/'tasks'/t/'arms'/a/'final_summary.json').exists() for a in ARMS) for t in TASKS)
    errors=[]
    if executor_reserved>81 or generation_reserved>12:errors.append('budget_cap_exceeded')
    if len(trials)!=executor_reserved:errors.append('unfinished_reserved_executor_slots')
    counts=Counter(r.get('sandbox_id') for r in trials if r.get('sandbox_id'))
    if any(n>1 for n in counts.values()):errors.append('duplicate_sandbox_ids')
    if (ROOT/'integrity_abort.json').exists():errors.append('global_integrity_abort')
    return {'protocol_complete':stage_complete and not errors,'errors':errors,
        'executor_reserved':executor_reserved,'generation_reserved':generation_reserved,
        'generation_tokens':totals(generations),'executor_tokens':totals(trials),
        'generation_attempts':generations,'phases':results,'trials':trials}


def main():
    a=build();write(ROOT/'results.json',a)
    lines=['# Three-task bundled historical-input ablation','',f"Fixed protocol complete: {a['protocol_complete']}.",
        f"Reserved executor trials: {a['executor_reserved']}/81; generation slots: {a['generation_reserved']}/12.",'',
        'The shared control is a uniformly sanitized original Skill; its learned body and resources were removed in both arms and the control. A uses all selected original histories/memory/loaded skills; B uses failure structure only. This joint deletion also loses useful diagnosis, so the result does not isolate leakage or individual factors. No final set is fed back to generation. The extra two tasks were seen in earlier diagnosis and are not blind tests.','',
        '| Task | Phase | Rewards (null = unresolved) | Valid | Successes | Seconds | Rounds |',
        '| --- | --- | --- | --- | --- | --- | --- |']
    for r in a['phases']:
        s=r['summary'];lines.append(f"| {r['task']} | {r['phase']} | {json.dumps(s['rewards'])} | {s['valid']} | {s['successes']} | {json.dumps(s['durations_seconds'])} | {json.dumps(s['rounds'])} |")
    lines+=['',f"Generation observed tokens: {a['generation_tokens']['observed_total']} ({a['generation_tokens']['unknown_records']} unknown records).",
            f"Executor observed tokens: {a['executor_tokens']['observed_total']} ({a['executor_tokens']['unknown_records']} unknown records).",'',
            '| Task | Arm | Candidate | Generation status | Seconds | Response model |','| --- | --- | --- | --- | --- | --- |']
    for g in a['generation_attempts']:lines.append(f"| {g['task']} | {g['arm']} | {g['candidate']} | {g['status']} | {g.get('seconds')} | {g.get('response_model')} |")
    lines+=['','All failures and unresolved trials, exact requests, autonomous packages and raw traces remain archived. Resource path references are recorded as evidence and do not alone prove successful execution. Full audited evidence and source limitations are in `input_audit.md`, `runner_preaudit.md`, `protocol.md` and `results.json`.','']
    (ROOT/'report.md').write_text('\n'.join(lines))
    print(json.dumps({'complete':a['protocol_complete'],'executor_reserved':a['executor_reserved'],'generation_reserved':a['generation_reserved'],'errors':a['errors']}))
if __name__=='__main__':main()
