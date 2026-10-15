"""Forward existing credentials to a bounded detached study process over SSH stdin."""
import argparse
import importlib.util
import json
from pathlib import Path
import shlex
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--action', choices=['generate', 'execute'], default='generate')
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location('credential_loader', repo / 'scripts/desktop_tb21_remote.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    qwen = module.load(Path.home() / 'Desktop/.env')
    method = module.load(Path.home() / 'Desktop/.env_deepseek')
    env = {'OPENAI_API_KEY': method['OPENAI_API_KEY'] if args.action == 'generate' else qwen['OPENAI_API_KEY'],
        'TB21_EXECUTOR_API_KEY': qwen['OPENAI_API_KEY'], 'E2B_API_KEY': qwen['E2B_API_KEY'],
        'E2B_DOMAIN': 'ap-beijing.tencentags.com', 'E2B_VALIDATE_API_KEY': 'false',
        'TBENCH_TENCENT_ENV_FILE': '/dev/null', 'TBENCH_PERSIST_SANDBOXES': '0',
        'TBENCH_E2B_RELAY_TEMPLATE': 'sdt-28lb4d9k', 'TBENCH_E2B_RELAY_USE_TEMPLATE_DEFAULT': '1',
        'TBENCH_E2B_RELAY_METADATA_JSON': '{"x-mounts":"[]"}',
        'TBENCH_RELAY_PROVIDER_BASE': 'https://llm-center.modelbest.co/v1',
        'EXPE_LLM_TIMEOUT_SECONDS': '1800', 'EXPE_LLM_RETRIES': '0'}
    remote = ('import json,os,subprocess,sys; from pathlib import Path; '
        'os.environ.update(json.load(sys.stdin)); '
        f'root=Path({args.root!r}); action={args.action!r}; '
        'marker=root/(action+"_dispatch.json"); '
        'assert not marker.exists(), "Already dispatched"; '
        'os.environ.update(TMPDIR=str(root/"tmp"),PYTHONPYCACHEPREFIX=str(root/"cache/pycache")); '
        '(root/"tmp").mkdir(exist_ok=True); '
        'log=(root/(action+".log")).open("a"); '
        'script=root/("run.py" if action=="generate" else "execute.py"); '
        'argv=[sys.executable,str(script)]+(["generate"] if action=="generate" else [])+["--root",str(root)]; '
        'p=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,env=os.environ,start_new_session=True); '
        'marker.write_text(json.dumps({"pid":p.pid,"no_restart":True})); '
        'print(json.dumps({"pid":p.pid,"action":action,"root":str(root)}))')
    command = '/data2/liyishan/SkillExpand-tb21/SkillExpand/.venv2/bin/python -c ' + shlex.quote(remote)
    result = subprocess.run(['ssh', 'jinan40', command], input=json.dumps(env), capture_output=True, text=True)
    output = result.stdout + result.stderr
    for value in [method['OPENAI_API_KEY'], qwen['OPENAI_API_KEY'], qwen['E2B_API_KEY']]:
        output = output.replace(value, '[REDACTED]')
    print(output, end='')
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
