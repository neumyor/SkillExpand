"""Dispatch the frozen pilot with live credentials through SSH stdin only."""
import importlib.util
import json
from pathlib import Path
import shlex
import subprocess


def main():
    repository = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location('desktop_credentials', repository / 'scripts/desktop_tb21_remote.py')
    desktop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(desktop)
    executor = desktop.load(Path.home() / 'Desktop/.env')
    method = desktop.load(Path.home() / 'Desktop/.env_deepseek')
    env = {'TB21_EXECUTOR_API_KEY': executor['OPENAI_API_KEY'],
           'TB21_METHOD_API_KEY': method['OPENAI_API_KEY'], 'E2B_API_KEY': executor['E2B_API_KEY']}
    remote = '''import json,os,runpy,sys
os.environ.update(json.load(sys.stdin))
root='/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/library-pilot-20261014-262144'
sys.path[:0]=[root,root+'/code']
runpy.run_path(root+'/runner.py',run_name='__main__')
'''
    command = '/data2/liyishan/SkillExpand-tb21/SkillExpand/.venv2/bin/python -c ' + shlex.quote(remote)
    result = subprocess.run(['ssh', '-o', 'ServerAliveInterval=15', 'jinan40', command],
                            input=json.dumps(env), text=True, capture_output=True)
    output = result.stdout + result.stderr
    for value in env.values():
        output = output.replace(value, '[REDACTED]')
    print(output, end='')
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
