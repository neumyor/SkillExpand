"""Keep the shared capacity reservation alive until every fixed driver exits."""
import os
import time
from pathlib import Path
from state import ROOT,read,write

def alive(pid):
    p=Path('/proc')/str(pid)/'stat'
    if not p.exists():return False
    try:return p.read_text().split(') ',1)[1].split()[0]!='Z'
    except (OSError,IndexError):return False

def main():
    while not (ROOT/'dispatch.json').exists():time.sleep(1)
    drivers=read(ROOT/'dispatch.json')['drivers']
    while True:
        running=[d for d in drivers if alive(d['pid'])]
        write(ROOT/'status.json',{'status':'running' if running else 'finished_pending_audit',
            'stage':'input_ablation_fixed_protocol','workers':45 if running else 0,
            'pid':os.getpid(),'running_drivers':running})
        if not running:break
        time.sleep(5)
if __name__=='__main__':main()
