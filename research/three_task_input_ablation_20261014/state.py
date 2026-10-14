"""Fixed paths, byte snapshots and atomic records for this experiment."""
import fcntl
import json
import os
import threading
from pathlib import Path

ROOT=Path(__file__).resolve().parent
TASKS=('cancel-async-tasks','multi-source-data-merger','log-summary-date-ranges')
ARMS=('A','B')

def read(p):return json.loads(Path(p).read_text())
def write(p,x):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
    temp=p.with_suffix(p.suffix+f'.{os.getpid()}.{threading.get_ident()}.tmp')
    temp.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n');temp.replace(p)
def freeze_bytes(p,data):
    p=Path(p)
    if p.exists():
        if p.read_bytes()!=data:raise ValueError('Frozen bytes differ: '+str(p))
    else:p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
def task_root(task):
    if task not in TASKS:raise ValueError('Unknown task')
    return ROOT/'tasks'/task
def arm_root(task,arm):
    if arm not in ARMS:raise ValueError('Unknown arm')
    return task_root(task)/'arms'/arm

def verify_freeze():
    if (ROOT/'integrity_abort.json').exists():raise ValueError('Global sandbox identity violation; further calls blocked')
    manifest=read(ROOT/'manifest.json')
    if not manifest.get('preaudit_passed'):raise ValueError('Pre-run semantic/input audit missing')
    for row in read(ROOT/'bindings.json'):
        if (ROOT/row['current']).read_bytes()!=(ROOT/row['snapshot']).read_bytes():
            raise ValueError('Freeze drift: '+row['current'])

def reserve(directory,kind):
    directory=Path(directory)
    with (ROOT/'.ledger.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if (directory/'reserved.json').exists():return False
        rel=directory.relative_to(ROOT)
        parts=rel.parts
        if len(parts)<4 or parts[:1]!=('tasks',) or parts[1] not in TASKS:raise ValueError('Invalid slot')
        if kind=='executor':
            if parts[2]=='baseline':valid=len(parts)==4 and parts[3] in {f'{i:02d}' for i in range(1,6)}
            else:
                valid=(parts[2]=='arms' and parts[3] in ARMS and
                       ((len(parts)==7 and parts[4] in ('candidate-001','candidate-002') and parts[5]=='dev' and parts[6] in ('01','02','03')) or
                        (len(parts)==6 and parts[4]=='final' and parts[5] in {f'{i:02d}' for i in range(1,6)})))
            if not valid:raise ValueError('Executor slot outside fixed protocol')
            total=sum(read(p)['kind']=='executor' for p in (ROOT/'tasks').rglob('reserved.json'))
            if total>=81:raise ValueError('81 trial cap exceeded')
        elif kind=='generation':
            if not (len(parts)==5 and parts[2]=='arms' and parts[3] in ARMS and parts[4] in ('candidate-001','candidate-002')):
                raise ValueError('Generation slot outside fixed protocol')
        else:raise ValueError('Unknown reservation type')
        write(directory/'reserved.json',{'kind':kind,'slot':str(rel)})
        return True
