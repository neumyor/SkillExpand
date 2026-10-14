"""Use the existing credential loader and SSH stdin for isolated research."""
import argparse
import importlib.util
import json
import shlex
import subprocess
from pathlib import Path


REPO = Path('/Users/a1/Documents/New project/SkillExpand')
spec = importlib.util.spec_from_file_location('desktop', REPO / 'scripts/desktop_tb21_remote.py')
desktop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(desktop)
parser = argparse.ArgumentParser()
parser.add_argument('action', choices=('generate', 'dev', 'accept', 'report'))
parser.add_argument('--candidate', type=int, default=1)
parser.add_argument('--batch', type=int, default=1)
parser.add_argument('--runs', type=int, default=3)
args = parser.parse_args()
qwen = desktop.load(Path.home() / 'Desktop/.env')
method = desktop.load(Path.home() / 'Desktop/.env_deepseek')
env = {'TB21_EXECUTOR_API_KEY': qwen['OPENAI_API_KEY'], 'TB21_METHOD_API_KEY': method['OPENAI_API_KEY'],
       'E2B_API_KEY': qwen['E2B_API_KEY'], 'E2B_DOMAIN': 'ap-beijing.tencentags.com',
       'E2B_VALIDATE_API_KEY': 'false', 'TBENCH_PERSIST_SANDBOXES': '0',
       'TBENCH_TENCENT_ENV_FILE': '/dev/null', 'TBENCH_E2B_RELAY_TEMPLATE': 'sdt-28lb4d9k',
       'TBENCH_E2B_RELAY_USE_TEMPLATE_DEFAULT': '1', 'TBENCH_E2B_RELAY_METADATA_JSON': '{"x-mounts":"[]"}',
       'EXPE_LLM_TIMEOUT_SECONDS': '1800', 'TB21_PIPELINE_CACHE': '1',
       'TB21_PIPELINE_CACHE_VERSION': 'pipeline-uv095-py3139-v6'}
remote = '''import json,os,runpy,sys
os.environ.update(json.load(sys.stdin))
root='/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/generic-skill-evolution-20261014'
repo='/data2/liyishan/SkillExpand-tb21/SkillExpand'
sys.path[:0]=[root,root+'/code',repo+'/scripts']
action=sys.argv[1]
sys.argv=[root+('/generate.py' if action=='generate' else '/evaluate.py')]+([] if action=='generate' else [action])+sys.argv[2:]
runpy.run_path(sys.argv[0],run_name='__main__')
'''
params = [args.action, '--candidate', str(args.candidate)]
if args.action != 'generate':
    params += ['--batch', str(args.batch), '--runs', str(args.runs)]
command = '/data2/liyishan/SkillExpand-tb21/SkillExpand/.venv2/bin/python -c ' + shlex.quote(remote) + ' ' + shlex.join(params)
result = subprocess.run(['ssh', '-o', 'ServerAliveInterval=15', 'jinan40', command],
                        input=json.dumps(env), text=True, capture_output=True)
output = result.stdout + result.stderr
for key in ('TB21_EXECUTOR_API_KEY', 'TB21_METHOD_API_KEY', 'E2B_API_KEY'):
    output = output.replace(env[key], '[REDACTED]')
print(output, end='')
raise SystemExit(result.returncode)
