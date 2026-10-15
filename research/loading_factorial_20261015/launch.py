"""Dispatch a prepared run, forwarding credentials only through SSH stdin."""
import argparse
import importlib.util
import json
from pathlib import Path
import shlex
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location('desktop_credentials', repository / 'scripts/desktop_tb21_remote.py')
    desktop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(desktop)
    values = desktop.load(Path.home() / 'Desktop/.env')
    env = {'TB21_EXECUTOR_API_KEY': values['OPENAI_API_KEY'], 'E2B_API_KEY': values['E2B_API_KEY']}
    remote = ('import json,os,runpy,sys; os.environ.update(json.load(sys.stdin)); '
              f'root={args.root!r}; sys.path[:0]=[root,root+"/code"]; '
              'runpy.run_path(root+"/runner.py",run_name="__main__")')
    command = '/data2/liyishan/SkillExpand-tb21/SkillExpand/.venv2/bin/python -c ' + shlex.quote(remote)
    result = subprocess.run(['ssh', 'jinan40', command], input=json.dumps(env), text=True, capture_output=True)
    output = result.stdout + result.stderr
    for value in env.values():
        output = output.replace(value, '[REDACTED]')
    print(output, end='')
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
