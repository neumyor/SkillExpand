#!/usr/bin/env python
"""Start a long command in its own session so it survives the launching shell.

Why not ``nohup ... & disown``
------------------------------
``disown`` only removes the job from the *shell's* job table.  The child stays in
the launcher's **process group**, so when the launching harness tears that group
down the driver dies with it -- measured in this workspace: a launcher's exit
killed a queued trainer and cost 1.7 hours of work.  macOS has no ``setsid(1)``
command, but ``os.setsid()`` is available, which is what this uses.

It also writes a pidfile and refuses to start when the pid in it is still alive,
so the same job cannot silently be started twice.

Usage:
    .venv/bin/python scripts/detach.py --pidfile logs/x.pid --log logs/x.log \
        -- .venv/bin/python -u scripts/whatever.py --flag
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--pidfile', required=True)
    ap.add_argument('--log', required=True)
    ap.add_argument('cmd', nargs=argparse.REMAINDER)
    args = ap.parse_args()

    cmd = args.cmd[1:] if args.cmd[:1] == ['--'] else args.cmd
    if not cmd:
        ap.error('no command given (use: detach.py --pidfile P --log L -- cmd ...)')

    pidfile = Path(args.pidfile)
    if pidfile.exists():
        old = int(pidfile.read_text().strip() or 0)
        if old and _alive(old):
            print(f'REFUSING: pid {old} from {pidfile} is still alive', file=sys.stderr)
            return 2

    log = open(args.log, 'ab')
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(f'{proc.pid}\n')
    print(f'started pid {proc.pid} (own session) -> {args.log}')
    return 0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


if __name__ == '__main__':
    sys.exit(main())
