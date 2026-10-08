#!/usr/bin/env python3
"""Campaign entry point; see ``skillexpand.campaign``.

``prepare`` freezes this checkout's ``src`` into ``<root>/code``.  Every other
action is handed to that campaign's frozen ``<root>/code/run_campaign.py``, so
no campaign action ever runs this checkout's live source.
"""
import argparse
import os
import sys
from pathlib import Path


def frozen_launcher(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('action', nargs='?')
    parser.add_argument('--root', type=Path)
    known, _ = parser.parse_known_args(argv)
    if known.action in (None, 'prepare') or known.root is None:
        return None
    launcher = known.root.resolve() / 'code' / 'run_campaign.py'
    return launcher if launcher.is_file() else None


if __name__ == '__main__':
    launcher = frozen_launcher(sys.argv[1:])
    if launcher is not None:
        os.execv(sys.executable, [sys.executable, str(launcher), *sys.argv[1:]])
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from skillexpand.campaign import main
    raise SystemExit(main())
