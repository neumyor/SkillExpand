"""Run only unresolved slots outside persisted timeout score overrides."""
import argparse
import fcntl
import json
import os
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from apply_tb21_task_timeout_zero import apply
from run_tb21_aligned import METHOD, QWEN, run_slot
from run_tb21_empirical import active_worker_reservation
from skillexpand.persistence.artifacts import load_cold_start
from skillexpand.runtime.llm_relay import relay_from_env
from summarize_tb21_empirical import read, write


def stage_worker(root, slots):
    identity = read(root / 'alignment.json')
    relay = selector_relay = None
    try:
        os.environ.update(MODEL_NAME=identity['executor_model'], TBENCH_PERSIST_SANDBOXES='0',
                          TBENCH_TENCENT_ENV_FILE='/dev/null', EXPE_LLM_MAX_TOKENS='65536')
        relay = relay_from_env()
        url = relay.start()
        os.environ['TB21_METHOD_API_KEY'] = os.environ['OPENAI_API_KEY']
        if identity['executor_model'] == QWEN:
            os.environ['OPENAI_API_KEY'] = os.environ['TB21_EXECUTOR_API_KEY']
        transport = relay.transport
        if identity.get('selector_model') == QWEN:
            selector_relay = relay_from_env()
            selector_relay.start()
            transport = selector_relay.transport
        os.environ.update(EXPE_CONFIG_FILE=str(root / 'config.json'),
                          EXPE_TASK_FILE=str(Path.cwd() / 'src/skillexpand/data/terminalbench/tb21.json'),
                          EXPE_LLM_BASE_URL=url, OPENAI_API_BASE=url, MODEL_API_BASE=url,
                          EXPE_LLM_RELAY_REQUIRED='1')
        cfg, _, skills, _ = load_cold_start(root)
        tasks = read(root / 'tasks.json')
        apply(root)
        def execute(slot):
            overrides = read(root / 'task_timeout_score_overrides.json')['overrides']
            if slot in overrides or read(root / f'slots/{slot}/record.json')['status'] == 'valid':
                return
            task, attempt = map(int, slot.split('-'))
            latest = sorted((root / f'slots/{slot}/requests').glob('*'))
            if latest and not list(latest[-1].glob('jobs/*/*/result.json')):
                deadline = time.monotonic() + 10800
                while not list(latest[-1].glob('jobs/*/*/result.json')):
                    if time.monotonic() >= deadline:
                        raise ValueError(f'Unfinished request wait expired: {slot}')
                    time.sleep(15)
                apply(root)
                if read(root / f'slots/{slot}/record.json')['status'] == 'valid':
                    return
            return run_slot(root, cfg, skills, tasks, transport, task, attempt,
                            identity['executor_model'], identity.get('selector_model', METHOD))
        with ThreadPoolExecutor(max_workers=len(slots)) as pool:
            list(pool.map(execute, slots))
        apply(root)
    finally:
        if selector_relay:
            selector_relay.close()
        if relay:
            relay.close()


def process_identity(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    except FileNotFoundError:
        return None
    return None if fields[0] == 'Z' else fields[19]


def cache_settings(version):
    if not version:
        return None
    adapter = Path('/data2/liyishan/tb21-tencent-skill/python')
    sys.path.insert(0, str(adapter))
    from tencent_pipeline_cache import load_bundle
    _, manifest = load_bundle(version)
    return {'enabled': True, 'version': version, 'status': manifest['status'],
            'prepare_timeout_sec': 1200, 'verifier_timeout_sec': 900}


def launch(qwen_pid, pipeline=False, finished_only=False, cache_version=None, wait_controls=None):
    base = Path.cwd()
    prior = dict(x.decode().split('=', 1) for x in
                 Path(f'/proc/{qwen_pid}/environ').read_bytes().split(b'\0') if b'=' in x)
    env = prior.copy()
    qwen_key = prior.get('TB21_EXECUTOR_API_KEY')
    if not qwen_key:
        raise ValueError('Verified executor credential unavailable')
    for line in sys.stdin.read().splitlines():
        line = line.strip().removeprefix('export ')
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            values = shlex.split(value)
            env[key.strip()] = values[0] if values else ''
    if not env.get('OPENAI_API_KEY'):
        raise ValueError('Method credential unavailable')
    env.update(PYTHONPATH='src:scripts', TB21_TOTAL_WORKER_LIMIT='250',
               TB21_EXECUTOR_API_KEY=qwen_key, TBENCH_E2B_RELAY_TEMPLATE='sdt-28lb4d9k',
               TBENCH_E2B_RELAY_USE_TEMPLATE_DEFAULT='1',
               TBENCH_E2B_RELAY_METADATA_JSON='{"x-mounts":"[]"}',
               TB21_PACKAGE_MIRROR_URL='https://mirrors.tencent.com',
               TBENCH_PERSIST_SANDBOXES='0', TBENCH_TENCENT_ENV_FILE='/dev/null')
    for key in ('EXPE_LLM_BASE_URL', 'OPENAI_API_BASE', 'MODEL_API_BASE', 'TB21_METHOD_API_KEY'):
        env.pop(key, None)
    cache = cache_settings(cache_version)
    if cache:
        if not pipeline:
            raise ValueError('Pipeline cache requires --pipeline')
        env.update(TB21_PIPELINE_CACHE='1', TB21_PIPELINE_CACHE_VERSION=cache_version)
    if wait_controls:
        if not cache:
            raise ValueError('Deferred pipeline launch requires an explicit cache version')
        return queue_cached_launch(base, env, wait_controls, cache)
    return launch_ready(base, env, pipeline, finished_only, cache)


def launch_ready(base, env, pipeline, finished_only, cache=None):
    targets = {
        'tb21-e3-e1aligned-free-library-20261009': ['82-1'],
        'tb21-e5-e1aligned-free-library-20261009': ['51-1', '82-3'],
        'tb21-e6-e1aligned-qwen-experience-deepseek-evolver-20261009': ['66-2'],
    }
    if pipeline:
        targets = {
            'tb21-e3-e1aligned-free-library-20261009': ['81-1', '81-3'],
            'tb21-e5-e1aligned-free-library-20261009': ['81-1', '81-3'],
            'tb21-e6-e1aligned-qwen-experience-deepseek-evolver-20261009': ['81-2'],
        }
    for name, slots in targets.items():
        root = base / 'runs' / name
        apply(root)
        overrides = read(root / 'task_timeout_score_overrides.json')['overrides']
        targets[name] = [s for s in slots if s not in overrides and
                         read(root / f'slots/{s}/record.json')['status'] != 'valid']
        if finished_only:
            if cache:
                for slot in targets[name]:
                    latest = sorted((root / 'slots' / slot / 'requests').glob('*'))
                    if latest and not list(latest[-1].glob('jobs/*/*/result.json')):
                        raise ValueError(f'Unfinished request blocks cached launch: {name}/{slot}')
            targets[name] = [s for s in targets[name] if list(sorted(
                (root / 'slots' / s / 'requests').glob('*'))[-1].glob('jobs/*/*/result.json'))]
    workers = sum(map(len, targets.values()))
    if not workers:
        print('No unresolved target slots')
        return
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    control = base / 'runs' / f'tb21-policy-tail-{stamp}'
    control.mkdir()
    write(control / 'manifest.json', {'workers': workers, 'targets': targets,
                                     'pipeline_cache': cache,
                                     'timeout_override_slots_excluded': True, 'budget_resets': 0,
                                     'max_requests_per_slot_this_launch': 3,
                                     'request_counts_before': {name: {slot: len(list(
                                         (base / 'runs' / name / 'slots' / slot / 'requests').glob('*')))
                                         for slot in slots} for name, slots in targets.items()}})
    with (base / 'runs/.tb21-worker-reservations.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        reserved, _ = active_worker_reservation(base / 'runs', control)
        if reserved + workers > 250:
            raise ValueError('Worker limit exceeded')
        with (control / 'driver.log').open('ab') as log:
            proc = subprocess.Popen([sys.executable, __file__, '--control', str(control)],
                                    env=env, cwd=base, stdout=log, stderr=log, start_new_session=True)
        write(control / 'status.json', {'status': 'running', 'pid': proc.pid, 'workers': workers,
                                       'phase': 'targeted_tail', 'other_reserved': reserved})
    print(json.dumps({'control': str(control), 'pid': proc.pid, 'workers': workers, 'targets': targets}))
    return control, proc


def queue_cached_launch(base, env, controls, cache):
    # Keep this lock through the resulting launch, including its automatic retries.
    with (base / 'runs/.tb21-cached-pipeline-launch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        predecessors = []
        for control in controls:
            control = control.resolve()
            status = read(control / 'status.json')
            children = read(control / 'children.json')
            processes = [{'pid': pid, 'start_ticks': process_identity(pid)}
                         for pid in [status.get('pid'), *children.values()] if pid]
            predecessors.append({'control': str(control), 'processes': processes})
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        queue = base / 'runs' / f'tb21-pipeline-cache-wait-{stamp}'
        queue.mkdir()
        write(queue / 'manifest.json', {'workers': 0, 'pipeline_cache': cache,
                                       'predecessors': predecessors, 'budget_resets': 0,
                                       'max_requests_per_slot_this_launch': 3})
        with (queue / 'driver.log').open('ab') as log:
            proc = subprocess.Popen([sys.executable, __file__, '--deferred-control', str(queue),
                                     '--queue-lock-fd', str(lock.fileno())], env=env, cwd=base,
                                    stdout=log, stderr=log, start_new_session=True,
                                    pass_fds=(lock.fileno(),))
        write(queue / 'status.json', {'status': 'running', 'pid': proc.pid, 'workers': 0,
                                     'phase': 'waiting_for_predecessors'})
    print(json.dumps({'control': str(queue), 'pid': proc.pid, 'workers': 0,
                      'phase': 'waiting_for_predecessors', 'pipeline_cache': cache}))


def deferred_launch(control, lock_fd):
    with os.fdopen(lock_fd) as lock:
        try:
            manifest = read(control / 'manifest.json')
            deadline = time.monotonic() + 12 * 3600
            while any(p['start_ticks'] is not None and
                      process_identity(p['pid']) == p['start_ticks']
                      for item in manifest['predecessors'] for p in item['processes']):
                if time.monotonic() >= deadline:
                    raise TimeoutError('Predecessor wait exceeded 12 hours; no requests launched')
                time.sleep(15)
            cache = cache_settings(manifest['pipeline_cache']['version'])
            launched = launch_ready(control.parent.parent, os.environ.copy(), True, True, cache)
            if not launched:
                write(control / 'status.json', {'status': 'complete', 'workers': 0,
                                               'phase': 'no_unresolved_target_slots'})
                return
            launched_control, proc = launched
            write(control / 'status.json', {'status': 'running', 'pid': os.getpid(), 'workers': 0,
                                           'phase': 'cached_tail_running',
                                           'launched_control': str(launched_control)})
            code = proc.wait()
            outcome = read(launched_control / 'status.json')
            write(control / 'status.json', {'status': outcome['status'] if code == 0 else 'needs_attention',
                                           'workers': 0, 'exit_code': code,
                                           'launched_control': str(launched_control)})
        except Exception as exc:
            write(control / 'status.json', {'status': 'needs_attention', 'workers': 0,
                                           'error_type': type(exc).__name__, 'error': str(exc)})
            raise


def supervise(control):
    manifest = read(control / 'manifest.json')
    children = []
    for name, slots in manifest['targets'].items():
        if not slots:
            continue
        root = control.parent / name
        with (control / f'{name}.log').open('ab') as log:
            child = subprocess.Popen([sys.executable, __file__, '--stage-root', str(root),
                                      '--slots', *slots], stdout=log, stderr=log)
        children.append((name, child))
    write(control / 'children.json', {name: child.pid for name, child in children})
    codes = {name: child.wait() for name, child in children}
    panels = {name: apply(control.parent / name) for name in manifest['targets']}
    remaining = {name: panel['unresolved_slots'] for name, panel in panels.items()}
    write(control / 'status.json', {'status': 'complete' if not any(codes.values()) and
                                   not any(remaining.values()) else 'needs_attention',
                                   'workers': manifest['workers'], 'exit_codes': codes,
                                   'remaining_slots': remaining})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--qwen-env-pid', type=int)
    parser.add_argument('--control', type=Path)
    parser.add_argument('--stage-root', type=Path)
    parser.add_argument('--slots', nargs='+')
    parser.add_argument('--pipeline', action='store_true')
    parser.add_argument('--retry-finished-only', action='store_true')
    parser.add_argument('--pipeline-cache-version')
    parser.add_argument('--wait-for-controls', type=Path, nargs='+')
    parser.add_argument('--deferred-control', type=Path)
    parser.add_argument('--queue-lock-fd', type=int)
    args = parser.parse_args()
    if args.deferred_control:
        deferred_launch(args.deferred_control, args.queue_lock_fd)
    elif args.stage_root:
        stage_worker(args.stage_root, args.slots)
    elif args.control:
        supervise(args.control)
    else:
        launch(args.qwen_env_pid, pipeline=args.pipeline, finished_only=args.retry_finished_only,
               cache_version=args.pipeline_cache_version, wait_controls=args.wait_for_controls)
