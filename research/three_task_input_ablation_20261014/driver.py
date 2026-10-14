"""Fixed baseline or two-candidate arm; no final feedback or replacement trials."""
import argparse
import fcntl
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
ROOT=Path(__file__).resolve().parent
sys.path[:0]=[str(ROOT),str(ROOT/'code')]
from state import TASKS,ARMS,read,write,freeze_bytes,verify_freeze,task_root,arm_root
from skillexpand import schema as S


def select(candidates):
    usable=[r for r in candidates if r['generated']]
    return min(usable,key=lambda r:(-r['successes'],r['number']))['number'] if usable else None

def main():
    p=argparse.ArgumentParser();p.add_argument('--task',choices=TASKS,required=True);p.add_argument('--arm',choices=ARMS);p.add_argument('--baseline',action='store_true');args=p.parse_args()
    if args.baseline==bool(args.arm):raise ValueError('Choose baseline or arm')
    os.environ['ABLATION_TASK_ROOT']=str(task_root(args.task))
    from execution import run
    from generation import generate
    folder=task_root(args.task)/'baseline' if args.baseline else arm_root(args.task,args.arm)
    folder.mkdir(parents=True,exist_ok=True)
    with (folder/'driver.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        verify_freeze()
        if args.baseline:
            skill=S.from_dict(S.Skill,read(task_root(args.task)/'frozen/seed.json'))
            with ThreadPoolExecutor(max_workers=5) as pool:
                records=list(pool.map(lambda i:run(folder/f'{i:02d}',skill),range(1,6)))
            write(folder/'summary.json',{'rewards':[r.get('reward') if r['status']=='valid' else None for r in records],
                                        'valid':sum(r['status']=='valid' for r in records),'successes':sum(r['status']=='valid' and r.get('reward')==1 for r in records)})
        else:
            candidates=[]
            for number in (1,2):
                candidate=folder/f'candidate-{number:03d}'
                generated=generate(args.task,args.arm,number)
                records=[]
                if generated:
                    skill=S.from_dict(S.Skill,read(candidate/'skill.json'))
                    with ThreadPoolExecutor(max_workers=3) as pool:
                        records=list(pool.map(lambda i:run(candidate/'dev'/f'{i:02d}',skill,True),range(1,4)))
                row={'number':number,'generated':generated,'successes':sum(r['status']=='valid' and r.get('reward')==1 for r in records),
                     'valid':sum(r['status']=='valid' for r in records),'rewards':[r.get('reward') if r['status']=='valid' else None for r in records]}
                write(candidate/'development_summary.json',row);candidates.append(row)
            # Re-read persisted identities/results before making the irreversible final selection.
            verify_freeze()
            for row in candidates:
                if row['generated']:
                    dev=folder/f"candidate-{row['number']:03d}"/'dev'
                    latest=[read(dev/f'{i:02d}'/'record.json') for i in range(1,4)]
                    row['successes']=sum(r['status']=='valid' and r.get('identity_valid') and r.get('reward')==1 for r in latest)
                    row['valid']=sum(r['status']=='valid' and r.get('identity_valid') for r in latest)
                    row['rewards']=[r.get('reward') if r['status']=='valid' and r.get('identity_valid') else None for r in latest]
            selection={'rule':'max valid dev successes, tie earliest; no usable package skips final',
                       'candidates':candidates,'selected':select(candidates)}
            freeze_bytes(folder/'selection.json',(__import__('json').dumps(selection,ensure_ascii=False,indent=2)+'\n').encode())
            if selection['selected'] is None:
                write(folder/'final_summary.json',{'status':'no_usable_candidate','attempts':0});return
            source=folder/f"candidate-{selection['selected']:03d}"/'skill.json'
            freeze_bytes(folder/'final/skill.json',source.read_bytes())
            skill=S.from_dict(S.Skill,read(folder/'final/skill.json'))
            with ThreadPoolExecutor(max_workers=5) as pool:
                records=list(pool.map(lambda i:run(folder/'final'/f'{i:02d}',skill),range(1,6)))
            freeze_bytes(folder/'final/skill.json',source.read_bytes())
            verify_freeze()
            write(folder/'final_summary.json',{'status':'complete','attempts':5,'selected':selection['selected'],
                      'rewards':[r.get('reward') if r['status']=='valid' else None for r in records],
                      'successes':sum(r['status']=='valid' and r.get('reward')==1 for r in records),
                      'valid':sum(r['status']=='valid' for r in records)})
        print(__import__('json').dumps({'task':args.task,'phase':'baseline' if args.baseline else args.arm,'complete':True}),flush=True)

if __name__=='__main__':main()
