"""Compact read-only progress; never print raw commands or credentials."""
import json
from collections import Counter
from pathlib import Path
from state import ROOT,TASKS,ARMS,read

def main():
    rows=[]
    for task in TASKS:
        root=ROOT/'tasks'/task
        for phase,folder in [('baseline',root/'baseline')]+[(a,root/'arms'/a) for a in ARMS]:
            records=[]
            for p in folder.rglob('record.json'):
                try:records.append(read(p))
                except (json.JSONDecodeError,OSError):pass
            gens=[{'slot':p.parent.name,**read(p)} for p in sorted(folder.glob('candidate-*/generation_status.json'))]
            rows.append({'task':task,'phase':phase,'finished_trials':len(records),
                'valid':sum(r['status']=='valid' for r in records),'successes':sum(r['status']=='valid' and r.get('reward')==1 for r in records),
                'unresolved':sum(r['status']!='valid' for r in records),'generations':gens,
                'final':read(folder/'final_summary.json') if (folder/'final_summary.json').exists() else None})
    dispatch=read(ROOT/'dispatch.json') if (ROOT/'dispatch.json').exists() else {}
    status=read(ROOT/'status.json') if (ROOT/'status.json').exists() else {}
    print(json.dumps({'status':status.get('status'),'running_drivers':len(status.get('running_drivers',[])),
        'reserved_trials':sum(read(p)['kind']=='executor' for p in (ROOT/'tasks').rglob('reserved.json')),
        'integrity_abort':(ROOT/'integrity_abort.json').exists(),'phases':rows},ensure_ascii=False))
if __name__=='__main__':main()
