"""Use the authorized credential loader; continuation credentials stay in stdin."""
import importlib.util
import json
from pathlib import Path
import shlex
import subprocess


def main():
    repo = Path('/Users/a1/Documents/New project/SkillExpand')
    spec = importlib.util.spec_from_file_location('desktop', repo/'scripts/desktop_tb21_remote.py')
    desktop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(desktop)
    qwen = desktop.load(Path.home()/'Desktop/.env')
    method = desktop.load(Path.home()/'Desktop/.env_deepseek')
    env = {'TB21_EXECUTOR_API_KEY': qwen['OPENAI_API_KEY'], 'TB21_METHOD_API_KEY': method['OPENAI_API_KEY'],
        'E2B_API_KEY': qwen['E2B_API_KEY'], 'E2B_DOMAIN': 'ap-beijing.tencentags.com',
        'E2B_VALIDATE_API_KEY': 'false', 'TBENCH_PERSIST_SANDBOXES': '0',
        'TBENCH_TENCENT_ENV_FILE': '/dev/null', 'TBENCH_E2B_RELAY_TEMPLATE': 'sdt-28lb4d9k',
        'TBENCH_E2B_RELAY_USE_TEMPLATE_DEFAULT': '1', 'TBENCH_E2B_RELAY_METADATA_JSON': '{"x-mounts":"[]"}',
        'EXPE_LLM_TIMEOUT_SECONDS': '1800', 'TB21_TOTAL_WORKER_LIMIT': '250',
        'TB21_PIPELINE_CACHE': '1', 'TB21_PIPELINE_CACHE_VERSION': 'pipeline-uv095-py3139-v6'}
    remote = '''import json,os,runpy,sys
os.environ.update(json.load(sys.stdin))
root='/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/three-task-input-ablation-20261014'
sys.path[:0]=[root,root+'/code']
runpy.run_path(root+'/resume_start.py',run_name='__main__')
'''
    command = '/data2/liyishan/SkillExpand-tb21/SkillExpand/.venv2/bin/python -c '+shlex.quote(remote)
    result = subprocess.run(['ssh', '-o', 'ServerAliveInterval=15', 'jinan40', command],
                            input=json.dumps(env), text=True, capture_output=True)
    output = result.stdout+result.stderr
    for key in ('TB21_EXECUTOR_API_KEY', 'TB21_METHOD_API_KEY', 'E2B_API_KEY'):
        output = output.replace(env[key], '[REDACTED]')
    print(output, end='')
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
