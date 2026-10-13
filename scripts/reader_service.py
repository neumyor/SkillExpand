#!/usr/bin/env python3
"""Start/stop only the dedicated reader process on the Linux data host."""

import argparse
import os
from pathlib import Path
import signal
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop", "status"])
    parser.add_argument("--runtime", type=Path, default=Path(__file__).resolve().parents[2] / "reader-runtime")
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    runtime = args.runtime.resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    pid_file = runtime / "reader.pid"
    pid = int(pid_file.read_text().strip()) if pid_file.exists() else None
    alive = False
    if pid:
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
            alive = b"skillexpand.reader.app" in cmd and str(runtime / "venv").encode() in cmd
        except OSError:
            pass
    if args.action == "status":
        print(f"reader running pid={pid}" if alive else "reader stopped")
    elif args.action == "stop":
        if alive:
            os.kill(pid, signal.SIGTERM)
            print(f"reader stopped pid={pid}")
        else:
            print("reader already stopped")
        pid_file.unlink(missing_ok=True)
    elif alive:
        print(f"reader already running pid={pid}")
    else:
        env = dict(os.environ, PYTHONPATH=str(repo / "src"), GRADIO_ANALYTICS_ENABLED="False", PYTHONUNBUFFERED="1")
        python = runtime / "venv/bin/python"
        if not python.exists():
            parser.error(f"Missing reader environment: {python}")
        with (runtime / "reader.log").open("ab") as log:
            child = subprocess.Popen([str(python), "-m", "skillexpand.reader.app", "--root", str(repo / "runs"),
                                      "--host", "127.0.0.1", "--port", str(args.port)], cwd=repo,
                                     env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        pid_file.write_text(str(child.pid) + "\n")
        print(f"reader starting pid={child.pid}; log={runtime / 'reader.log'}")


if __name__ == "__main__":
    main()
