"""Observe the actual continuation PIDs and maintain shared capacity accounting."""
import os
from pathlib import Path
import time
from state import ROOT, read, write


def alive(pid):
    try:
        return (Path('/proc')/str(pid)/'stat').read_text().split(') ', 1)[1].split()[0] != 'Z'
    except (OSError, IndexError):
        return False


def main():
    drivers = read(ROOT/'resume_dispatch.json')['drivers']
    while True:
        running = [d for d in drivers if alive(d['pid'])]
        write(ROOT/'status.json', {'status': 'running' if running else 'finished_pending_audit',
            'stage': 'input_ablation_fixed_protocol_continuation', 'workers': 45 if running else 0,
            'pid': os.getpid(), 'running_drivers': running,
            'original_dispatch': 'dispatch.json', 'continuation_dispatch': 'resume_dispatch.json'})
        if not running:
            break
        time.sleep(5)


if __name__ == '__main__':
    main()
