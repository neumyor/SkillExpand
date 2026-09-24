#!/usr/bin/env python3
"""Fork a SearchQA resume supervisor outside the caller's process group."""
import os
import sys

if os.fork():
    # Parent returns immediately; child owns the detached supervisor.
    raise SystemExit(0)
os.setsid()
os.umask(0o022)
os.execv(sys.executable, [sys.executable, '-u',
    os.path.join(os.path.dirname(__file__), 'launch_searchqa_resume.py'), *sys.argv[1:]])
