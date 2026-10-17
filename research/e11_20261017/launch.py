"""Single dispatch; credentials travel only through live SSH stdin/environment."""
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
    repo = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location('credentials', repo / 'scripts/desktop_tb21_remote.py')
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    qwen = loader.load(Path.home() / 'Desktop/.env')
    method = loader.load(Path.home() / 'Desktop/.env_deepseek')
    env = {'OPENAI_API_KEY': method['OPENAI_API_KEY'], 'TB21_METHOD_API_KEY': method['OPENAI_API_KEY'],
           'TB21_EXECUTOR_API_KEY': qwen['OPENAI_API_KEY'], 'E2B_API_KEY': qwen['E2B_API_KEY']}
    remote = ('import json,os,subprocess,sys; from pathlib import Path; '
        'os.environ.update(json.load(sys.stdin)); '
        f'root=Path({args.root!r}); '
        'fd=os.open(root/"dispatch.json",os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o644); '
        'env=json.loads((root/"frozen/environment.json").read_text()); os.environ.update(env); '
        'os.environ.update(TMPDIR=str(root/"tmp"),PYTHONPYCACHEPREFIX=str(root/"cache")); '
        '(root/"tmp").mkdir(exist_ok=True); '
        'log=(root/"supervisor.log").open("a"); '
        'p=subprocess.Popen([sys.executable,str(root/"driver.py"),"evolve","--root",str(root)],'
        'stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,env=os.environ,start_new_session=True); '
        'stat=Path("/proc",str(p.pid),"stat").read_text(); fields=stat[stat.rfind(")")+2:].split(); '
        'record={"pid":p.pid,"start_time_ticks":fields[19],"boot_id":Path("/proc/sys/kernel/random/boot_id").read_text().strip(),"root":str(root),"no_restart":True}; '
        'os.write(fd,json.dumps(record).encode()); os.close(fd); print(json.dumps(record))')
    command = '/data2/liyishan/SkillExpand-tb21/SkillExpand/.venv2/bin/python -c ' + shlex.quote(remote)
    result = subprocess.run(['ssh', 'jinan40', command], input=json.dumps(env), text=True, capture_output=True)
    output = result.stdout + result.stderr
    for secret in env.values():
        output = output.replace(secret, '[REDACTED]')
    print(output, end='')
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
