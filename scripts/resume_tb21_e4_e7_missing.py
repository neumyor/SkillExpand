"""Resume only the two approved missing slots without entering E7 L2."""
import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

from omegaconf import OmegaConf
from skillexpand import schema as S
from skillexpand.l2.loop import RunLock
from skillexpand.persistence.artifacts import load_cold_start
from skillexpand.runtime.llm_relay import relay_from_env, coalesce_sse
import run_tb21_aligned as aligned
import run_tb21_empirical as empirical
from summarize_tb21_empirical import read, write, summarize

TARGETS = {
    'E4': ('tb21-e4-e3-final-library-empirical-20261010', 9, 2, 5),
    'E7': ('tb21-qwen-deepseek-library-qwen-selector-20261011', 67, 2, 3),
}


def worker(stage):
    name, task, attempt, previous = TARGETS[stage]
    root = (Path('runs') / name).resolve()
    requests = root / 'slots' / f'{task:02d}-{attempt}' / 'requests'
    with RunLock(root / ('run.pid' if stage == 'E4' else 'driver.pid')):
        if read(root / 'slots' / f'{task:02d}-{attempt}' / 'record.json')['status'] == 'valid':
            return
        if len(list(requests.iterdir())) != previous:
            raise ValueError('Request history changed since approved resume')
        for request in requests.iterdir():
            if not list(request.glob('jobs/*/*/result.json')) and not (request / 'error.json').exists():
                raise ValueError('Unfinished request blocks resume')
        aligned.reserve(root, stage, 16)
        write(root / 'status.json', {'stage': stage, 'status': 'running', 'pid': os.getpid(),
            'workers': 16, 'coverage': '266/267', 'phase': 'missing_slot_recovery'})
        relay = selector = None
        try:
            os.environ.update(TBENCH_PERSIST_SANDBOXES='0', TBENCH_TENCENT_ENV_FILE='/dev/null',
                              EXPE_LLM_MAX_TOKENS='65536', EXPE_LLM_TIMEOUT_SECONDS='1800',
                              EXPE_REVIEWER_RESPONSE_FORMAT='omit', EXPE_LLM_EXTRA_JSON='{"enable_thinking":true}')
            os.environ['OPENAI_API_KEY'] = os.environ['TB21_METHOD_API_KEY']
            relay = relay_from_env()
            url = relay.start()
            # Verify both independent routes before creating a new slot request.
            for model, transport in [(aligned.METHOD, relay.transport)]:
                value = coalesce_sse(transport.stream({'model': model, 'stream': True,
                    'max_tokens': 65536, 'messages': [{'role': 'user', 'content': 'Reply ready.'}]}))
                if not value.get('choices', [{}])[0].get('message', {}).get('content'):
                    raise ValueError('Method credential verification failed')
            os.environ['OPENAI_API_KEY'] = os.environ['TB21_EXECUTOR_API_KEY']
            selector = relay_from_env()
            selector.start()
            value = coalesce_sse(selector.transport.stream({'model': aligned.QWEN, 'stream': True,
                'max_tokens': 32, 'messages': [{'role': 'user', 'content': 'Reply ready.'}]}))
            if not value.get('choices', [{}])[0].get('message', {}).get('content'):
                raise ValueError('Qwen credential verification failed')
            os.environ.update(EXPE_CONFIG_FILE=str(root / 'config.json'),
                EXPE_TASK_FILE=str(root / 'tasks.json'), EXPE_LLM_BASE_URL=url,
                OPENAI_API_BASE=url, MODEL_API_BASE=url, EXPE_LLM_RELAY_REQUIRED='1')
            write(root / 'missing_slot_resume.json', {'stage': stage, 'slot': f'{task:02d}-{attempt}',
                'previous_requests': previous, 'max_new_requests': 3, 'budget_resets': 0,
                'credentials_verified': True, 'credentials_persisted': False,
                'pid': os.getpid(), 'l2_auto_start': False})
            if stage == 'E4':
                cfg = OmegaConf.create(read(root / 'config.json'))
                skills = [S.from_dict(S.Skill, s) for s in read(root / 'library.json')]
                if empirical.final_heads(Path(read(root / 'manifest.json')['source_run'])) != read(root / 'library.json'):
                    raise ValueError('Frozen library mismatch')
                empirical.execute_slot(cfg, read(root / 'tasks.json'), skills, root,
                                       relay.transport, task, attempt)
                report = summarize(root)
                complete = read(root / 'audit.json')['complete']
                write(root / 'status.json', {'stage': 'E4_empirical',
                    'status': 'complete' if complete else 'needs_attention',
                    'coverage': f"{report['empirical']['valid_attempts']}/267"})
            else:
                cfg, _, skills, _ = load_cold_start(root)
                tasks = read(root / 'tasks.json')
                aligned.run_slot(root, cfg, skills, tasks, selector.transport,
                                 task, attempt, aligned.QWEN, aligned.QWEN)
                aligned.collect_cards(root, tasks, skills, aligned.QWEN)
                write(root / 'status.json', {'stage': 'QD', 'experiment': 'E7',
                    'status': 'initial_panel_complete', 'coverage': '267/267',
                    'workers': 16, 'phase': 'awaiting_178_card_l2_validation'})
        except Exception as exc:
            message = str(exc)
            for key in ('OPENAI_API_KEY', 'TB21_METHOD_API_KEY', 'TB21_EXECUTOR_API_KEY', 'E2B_API_KEY'):
                message = message.replace(os.environ.get(key) or '\0', '[REDACTED]')
            write(root / 'status.json', {'stage': stage, 'status': 'needs_attention',
                'error': message[:1500], 'coverage': '266/267'})
        finally:
            if selector:
                selector.close()
            if relay:
                relay.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--credential-pid', type=int)
    parser.add_argument('--worker', choices=tuple(TARGETS))
    args = parser.parse_args()
    if args.worker:
        worker(args.worker)
        return
    if not args.credential_pid:
        parser.error('--credential-pid required')
    prior = dict(x.decode().split('=', 1) for x in
                 Path(f'/proc/{args.credential_pid}/environ').read_bytes().split(b'\0') if b'=' in x)
    values = {}
    for line in sys.stdin.read().splitlines():
        line = line.strip().removeprefix('export ')
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            parsed = shlex.split(value)
            values[key.strip()] = parsed[0] if parsed else ''
    env = os.environ.copy()
    env.update(OPENAI_API_KEY=prior['OPENAI_API_KEY'], TB21_METHOD_API_KEY=prior['OPENAI_API_KEY'],
        TB21_EXECUTOR_API_KEY=values['OPENAI_API_KEY'], E2B_API_KEY=prior['E2B_API_KEY'],
        PYTHONPATH='src:scripts', TB21_TOTAL_WORKER_LIMIT='250',
        TBENCH_E2B_RELAY_TEMPLATE='sdt-28lb4d9k', TBENCH_E2B_RELAY_USE_TEMPLATE_DEFAULT='1',
        TBENCH_E2B_RELAY_METADATA_JSON='{"x-mounts":"[]"}', TB21_PIPELINE_CACHE='1',
        TB21_PIPELINE_CACHE_VERSION='pipeline-uv095-py3139-v6')
    # Each worker independently acquires its run lock and the shared capacity lock.
    for stage, (name, _, _, _) in TARGETS.items():
        root = Path('runs') / name
        with (root / 'missing_slot_resume.log').open('ab') as log:
            proc = subprocess.Popen([sys.executable, __file__, '--worker', stage],
                env=env, stdout=log, stderr=log, start_new_session=True)
        print(stage, proc.pid, flush=True)


if __name__ == '__main__':
    main()
