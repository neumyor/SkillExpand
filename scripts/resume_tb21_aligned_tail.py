"""Resume stopped TB21 stages with verified process-only model credentials."""
import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from reconcile_tb21_empirical_timeouts import reconcile
from run_tb21_empirical import active_worker_reservation
from skillexpand.runtime.llm_relay import TencentSandboxTransport, coalesce_sse
from summarize_tb21_empirical import read, write


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--qwen-env-pid', type=int, required=True)
    args = parser.parse_args()
    root = Path.cwd()
    runs = root / 'runs'
    prior = dict(item.decode().split('=', 1) for item in
                 Path(f'/proc/{args.qwen_env_pid}/environ').read_bytes().split(b'\0') if b'=' in item)
    qwen_key = prior.get('TB21_EXECUTOR_API_KEY') or prior.get('OPENAI_API_KEY')
    env = os.environ.copy()
    for line in sys.stdin.read().splitlines():
        line = line.strip().removeprefix('export ')
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        values = shlex.split(value)
        env[key.strip()] = values[0] if values else ''
    method_key = env.get('OPENAI_API_KEY')
    if not method_key or not qwen_key:
        raise ValueError('Both model credential roles are required')
    env.update(PYTHONPATH='src', TB21_TOTAL_WORKER_LIMIT='250',
               TBENCH_E2B_RELAY_TEMPLATE='sdt-28lb4d9k',
               TBENCH_E2B_RELAY_USE_TEMPLATE_DEFAULT='1',
               TBENCH_E2B_RELAY_METADATA_JSON='{"x-mounts":"[]"}',
               TBENCH_PERSIST_SANDBOXES='0', TBENCH_TENCENT_ENV_FILE='/dev/null')
    if not env.get('E2B_API_KEY'):
        env['E2B_API_KEY'] = prior['E2B_API_KEY']
    os.environ.update(env)
    stages = [('E1', 'tb21-e1-empirical-final-library-20261008', 16),
              ('E3', 'tb21-e3-e1aligned-free-library-20261009', 92),
              ('E5', 'tb21-e5-e1aligned-free-library-20261009', 92),
              ('E6', 'tb21-e6-e1aligned-qwen-experience-deepseek-evolver-20261009', 50)]
    for _, name, _ in stages:
        status = read(runs / name / 'status.json')
        pid = status.get('pid')
        if status.get('status') == 'running' and pid and Path(f'/proc/{pid}').exists():
            raise ValueError('Tail recovery requires stopped drivers')
    reserved, _ = active_worker_reservation(runs, Path('/tmp/tb21-tail-preflight'))
    if reserved + sum(workers for _, _, workers in stages) > 250:
        raise ValueError('Insufficient global worker capacity')
    verified = []
    for model, credential in [('DEEPSEEK_up5zdj', method_key), ('qwen3.6-flash-distill', qwen_key)]:
        transport = None
        try:
            transport = TencentSandboxTransport(template='sdt-28lb4d9k', api_key=credential,
                api_base='https://llm-center.modelbest.co/v1', metadata={'x-mounts': '[]'},
                request_timeout=300)
            completion = coalesce_sse(transport.stream({'model': model, 'stream': True,
                'enable_thinking': True, 'max_tokens': 65536,
                'messages': [{'role': 'user', 'content': 'Reply with the single word ready.'}]}))
            choices = completion.get('choices') or []
            if not choices or not (choices[0].get('message', {}).get('content') or '').strip():
                raise ValueError('Credential preflight returned no valid completion')
            verified.append({'model': model, 'valid_completion': True,
                             'transport': 'tencent_e2b_relay'})
            print('credential_verified', model, flush=True)
        finally:
            if transport:
                transport.close()
    for stage, name, workers in stages:
        directory = runs / name
        before = read(directory / 'status.json')
        reconcile(directory, empirical_report=stage == 'E1')
        missing = [slot.parent.name for slot in sorted(directory.glob('slots/*/record.json'))
                   if read(slot).get('status') != 'valid']
        child_env = env.copy()
        child_env['OPENAI_API_KEY'] = qwen_key if stage == 'E1' else method_key
        if stage in ('E5', 'E6'):
            child_env['TB21_EXECUTOR_API_KEY'] = qwen_key
        command = [str(root / '.venv2/bin/python')]
        if stage == 'E1':
            command += ['scripts/run_tb21_empirical.py', '--stage', stage,
                        '--source', read(directory / 'manifest.json')['source_run']]
        else:
            command += ['scripts/run_tb21_aligned.py', '--stage', stage,
                        '--task-file', 'src/skillexpand/data/terminalbench/tb21.json']
            identity = read(directory / 'alignment.json')
            sources = identity['sources']
            if stage != 'E6':
                command += ['--deepseek-source', sources[0]]
            if stage in ('E5', 'E6'):
                command += ['--qwen-source', sources[-1]]
        command += ['--run-dir', str(directory), '--workers', str(workers)]
        with (directory / 'tail_recovery.log').open('ab') as log:
            process = subprocess.Popen(command, env=child_env, cwd=root,
                                       stdout=log, stderr=log, start_new_session=True)
        write(directory / 'tail_recovery.json', {'previous_status': before,
            'new_pid': process.pid, 'workers': workers, 'missing_slots': missing,
            'valid_slots_reused': True, 'budget_resets': 0, 'global_limit': 250,
            'credentials_persisted': False, 'credential_preflight': verified,
            'method_source': 'desktop_deepseek',
            'qwen_source': f'previous_verified_process:{args.qwen_env_pid}'})
        print(stage, process.pid, 'missing', missing, flush=True)


if __name__ == '__main__':
    main()
