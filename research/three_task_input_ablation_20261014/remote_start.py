"""Launch the nine fixed drivers detached; persist PIDs without credential argv."""
import fcntl
import os
import subprocess
import sys
import time
from state import ROOT,TASKS,ARMS,read,write,verify_freeze


def main():
    verify_freeze()
    with (ROOT/'.start.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (ROOT/'dispatch.json').exists():raise ValueError('Already dispatched; inspect existing drivers instead of starting again')
        sys.path.insert(0,str(ROOT.parents[1]/'scripts'))
        from run_tb21_empirical import active_worker_reservation,total_worker_limit
        runs=ROOT.parent
        with (runs/'.tb21-worker-reservations.lock').open('a') as capacity_lock:
            fcntl.flock(capacity_lock,fcntl.LOCK_EX)
            reserved,active=active_worker_reservation(runs,ROOT)
            limit=total_worker_limit()
            if limit!=read(ROOT/'manifest.json')['coordination_worker_limit']:raise ValueError('Coordination capacity config drift')
            if reserved+45>limit:raise ValueError('Insufficient shared capacity; no trials started')
            children=[]
            for task in TASKS:
                for arm in (None,*ARMS):
                    command=[sys.executable,str(ROOT/'driver.py'),'--task',task]+(['--baseline'] if arm is None else ['--arm',arm])
                    log=ROOT/'tasks'/task/('baseline_driver.log' if arm is None else f'{arm}_driver.log')
                    with log.open('a') as stream:
                        child=subprocess.Popen(command,env=os.environ.copy(),stdin=subprocess.DEVNULL,
                           stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
                    children.append({'task':task,'phase':arm or 'baseline','pid':child.pid,'log':str(log.relative_to(ROOT))})
            write(ROOT/'dispatch.json',{'at':time.time(),'drivers':children,'reserved':45,'already_reserved':reserved})
            with (ROOT/'monitor.log').open('a') as stream:
                monitor=subprocess.Popen([sys.executable,str(ROOT/'monitor.py')],env=os.environ.copy(),stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
            write(ROOT/'status.json',{'status':'running','stage':'input_ablation_fixed_protocol','workers':45,'pid':monitor.pid,'all_driver_pids':[c['pid'] for c in children]})
    print(__import__('json').dumps({'dispatched':len(children),'max_trials':81,'children':children}),flush=True)
if __name__=='__main__':main()
