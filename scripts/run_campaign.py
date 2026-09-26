#!/usr/bin/env python3
"""Frozen, resumable cold-start -> evolve x2 campaign for both benchmarks."""
import argparse
from collections import Counter
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

BENCHMARKS = ('searchqa', 'alfworld')
STAGES = ('cold-start', 'evolve-1', 'evolve-2')
WORKERS = {'searchqa': 128, 'alfworld': 32}
REQUEST_INTERVAL_SECONDS = 0.5
RETRY_DELAYS = (15, 60, 180)


def configured_runtime():
    names = ('EXPE_LLM_MODEL', 'ALFWORLD_PYTHON', 'EXPE_CAMPAIGN_OVERLAY',
             'ALFWORLD_DATA', 'ALFWORLD_CONFIG', 'ALFWORLD_BENCH_SRC')
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise ValueError('Source scripts/env.sh and set: ' + ', '.join(missing))
    values = {name: os.environ[name] for name in names}
    paths = {
        'python': Path(os.path.abspath(os.path.expanduser(values['ALFWORLD_PYTHON']))),
        'overlay': Path(os.path.abspath(os.path.expanduser(values['EXPE_CAMPAIGN_OVERLAY']))),
        'alfworld_data': Path(os.path.abspath(os.path.expanduser(values['ALFWORLD_DATA']))),
        'alfworld_config': Path(os.path.abspath(os.path.expanduser(values['ALFWORLD_CONFIG']))),
        'alfworld_bench_src': Path(os.path.abspath(os.path.expanduser(values['ALFWORLD_BENCH_SRC']))),
    }
    expected = {'python': 'file', 'overlay': 'dir', 'alfworld_data': 'dir',
                'alfworld_config': 'file', 'alfworld_bench_src': 'dir'}
    for key, kind in expected.items():
        if not (paths[key].is_file() if kind == 'file' else paths[key].is_dir()):
            raise FileNotFoundError(f'Configured {key} must be a {kind}: {paths[key]}')
    return {'model': values['EXPE_LLM_MODEL'],
            **{key: str(path) for key, path in paths.items()}}


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    with temp.open('w') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def validate_inputs(tasks, split):
    assignment = split.get('assignment', split)
    if set(map(int, assignment)) != set(range(len(tasks))):
        raise ValueError('Split must cover each task exactly once')
    counts = Counter(assignment.values())
    if set(counts) != {'source', 'admission', 'final'}:
        raise ValueError('All three disjoint splits are required')
    return dict(counts)


def prepare(root, inputs):
    repo = Path(__file__).resolve().parents[1]
    runtime = configured_runtime()
    if root.exists() and any(root.iterdir()):
        raise ValueError('Prepare requires a new campaign directory')
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(repo / 'src', root / 'code' / 'src',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.egg-info'))
    shutil.copyfile(__file__, root / 'code' / 'run_campaign.py')
    details = {}
    for benchmark in BENCHMARKS:
        task_path, split_path = inputs / f'{benchmark}-tasks.json', inputs / f'{benchmark}-split.json'
        tasks, split = read(task_path), read(split_path)
        counts = validate_inputs(tasks, split)
        for kind, source in (('tasks', task_path), ('split', split_path)):
            destination = root / 'inputs' / f'{benchmark}-{kind}.json'
            destination.parent.mkdir(exist_ok=True)
            shutil.copyfile(source, destination)
        assignment = split.get('assignment', split)
        # All smoke tasks come from full SOURCE. No final question is exposed
        # during implementation checks or used to select the smoke sample.
        selected = sorted(int(t) for t, part in assignment.items() if part == 'source')[:4]
        if len(selected) < 4:
            raise ValueError('Preflight needs four source tasks')
        save(root / 'inputs' / f'{benchmark}-preflight-tasks.json', [tasks[t] for t in selected])
        save(root / 'inputs' / f'{benchmark}-preflight-split.json',
             {'assignment': {'0': 'source', '1': 'source', '2': 'admission', '3': 'final'}})
        details[benchmark] = {'counts': counts, 'preflight_original_task_ids': selected,
                              'original_tasks': str(task_path), 'original_split': str(split_path)}
    files = {str(p.relative_to(root)): digest(p) for folder in ('code', 'inputs')
             for p in sorted((root / folder).rglob('*')) if p.is_file()}
    manifest = {
        'schema': 1, 'repo': str(repo), **runtime,
        'workers': WORKERS, 'evolve_rounds': 2, 'autonomous_attempts': 4,
        'batch_size': 50, 'candidate_count': 3, 'benchmarks': details,
        'request_interval_seconds': REQUEST_INTERVAL_SECONDS,
        'files': files, 'created': time.time(),
        'metric': 'Source-task first-autonomous-attempt success at cold start, evolve-1, and evolve-2; paired by task.',
        'comparison': 'Report each benchmark independently using the fixed three stages; no best-round selection or held-out execution.',
        'timeouts': {'request': 300, 'request_retries': 2, 'environment': 120, 'worker_progress': 3600},
    }
    save(root / 'manifest.json', manifest)
    return verify(root)


def verify(root):
    manifest = read(root / 'manifest.json')
    for relative, expected in manifest['files'].items():
        if digest(root / relative) != expected:
            raise ValueError(f'Frozen campaign file changed: {relative}')
    if (not manifest['model'] or manifest['workers'] != WORKERS or
            manifest['evolve_rounds'] != 2 or
            manifest['request_interval_seconds'] != REQUEST_INTERVAL_SECONDS):
        raise ValueError('Unexpected campaign protocol')
    for key, kind in (('python', 'file'), ('overlay', 'dir'), ('alfworld_data', 'dir'),
                      ('alfworld_config', 'file'), ('alfworld_bench_src', 'dir')):
        path = Path(manifest[key])
        if not (path.is_file() if kind == 'file' else path.is_dir()):
            raise FileNotFoundError(f'Missing configured {key}: {path}')
    for benchmark, settings in manifest['benchmarks'].items():
        counts = validate_inputs(read(root / 'inputs' / f'{benchmark}-tasks.json'),
                                 read(root / 'inputs' / f'{benchmark}-split.json'))
        if counts != settings['counts']:
            raise ValueError('Frozen split counts changed')
    return manifest


def environment(root):
    manifest = read(root / 'manifest.json')
    env = dict(os.environ)
    missing = [name for name in ('EXPE_LLM_BASE_URL', 'OPENAI_API_KEY') if not env.get(name)]
    if missing:
        raise ValueError('Source scripts/env.sh and set: ' + ', '.join(missing))
    for key in ('EXPE_CONFIG_FILE', 'EXPE_TASK_FILE', 'EXPE_LLM_EXTRA_JSON', 'OPENAI_API_BASE'):
        env.pop(key, None)
    timeout = manifest['timeouts']
    env.update(EXPE_LLM_MODEL=manifest['model'], EXPE_LLM_DISABLE_THINKING='1', EXPE_SHOW_ADMISSIBLE='1',
        PYTHONPATH=str(root / 'code/src') + os.pathsep + manifest['overlay'], PYTHONUNBUFFERED='1',
        ALFWORLD_DATA=manifest['alfworld_data'],
        ALFWORLD_CONFIG=manifest['alfworld_config'],
        ALFWORLD_BENCH_SRC=manifest['alfworld_bench_src'],
        TIKTOKEN_CACHE_DIR=str(Path(manifest['repo']) / '.cache-tiktoken'),
        MPLCONFIGDIR=str(Path(manifest['repo']) / '.mpl-cache'),
        OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1', TOKENIZERS_PARALLELISM='false',
        EXPE_LLM_TIMEOUT_SECONDS=str(timeout['request']), EXPE_LLM_RETRIES=str(timeout['request_retries']),
        EXPE_LLM_GATE_FILE=str(root / 'request-gate.state'),
        EXPE_LLM_REQUEST_INTERVAL_SECONDS=str(manifest['request_interval_seconds']),
        EXPE_ENV_TIMEOUT_SECONDS=str(timeout['environment']), EXPE_WORKER_TIMEOUT_SECONDS=str(timeout['worker_progress']))
    return env


def health(root):
    env = environment(root)
    model = read(root / 'manifest.json')['model']
    payload = {'model': model, 'messages': [{'role': 'user', 'content': 'Reply with OK.'}],
               'max_tokens': 8, 'temperature': 0, 'enable_thinking': False}
    request = urllib.request.Request(env['EXPE_LLM_BASE_URL'].rstrip('/') + '/chat/completions',
        data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + env['OPENAI_API_KEY']})
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=60) as response:
        value = json.loads(response.read())
    content = value['choices'][0]['message'].get('content', '')
    if not content.strip():
        raise ValueError('Health request returned empty content')
    result = {'requested_model': model, 'reported_model': value.get('model'),
              'seconds': round(time.monotonic() - started, 3), 'content': content, 'time': time.time()}
    save(root / 'health.json', result)
    return result


def stage_args(root, mode, benchmark, stage):
    manifest = read(root / 'manifest.json')
    workers = manifest['workers'][benchmark]
    suffix = '-preflight' if mode == 'preflight' else ''
    args = ['--benchmark', benchmark, '--run-dir', str(root / mode / benchmark / 'run'),
        '--task-file', str(root / 'inputs' / f'{benchmark}{suffix}-tasks.json'),
        '--split-file', str(root / 'inputs' / f'{benchmark}{suffix}-split.json'),
        '--workers', str(workers), '--discovery-workers', str(workers),
        '--evolve-l1-workers', str(workers), '--panel-workers', str(workers),
        '--autonomous-attempts', str(manifest['autonomous_attempts']), '--batch-size', str(manifest['batch_size']),
        '--candidate-count', str(manifest['candidate_count']), '--resume']
    if stage.startswith('evolve-'):
        args += ['--phase', 'evolve', '--evolve-rounds', stage.split('-')[1]]
    else:
        args += ['--phase', stage]
    return args


def audit_stage(root, mode, benchmark, stage):
    from omegaconf import OmegaConf
    from skillexpand.l1.adapters import resolve
    from skillexpand.l1.audit import audit_checkpoint, audit_usage
    from skillexpand.persistence.artifacts import load_cold_start
    from skillexpand.l2.audit import audit_round
    from skillexpand.evaluation.audit import audit_final
    run = root / mode / benchmark / 'run'
    cfg, plan, initial, _ = load_cold_start(run)
    if cfg.agent.llm != read(root / 'manifest.json')['model']:
        raise ValueError('Actual model differs from requested model')
    if stage == 'final':
        summary = read(run / 'summary.json')
        if summary['latest_evolution_round'] != 2:
            raise ValueError('Final preceded the second evolve round')
        finals = list((run / 'final').glob('*/summary.json'))
        if len(finals) != 1:
            raise ValueError('Final must contain exactly one evaluated library')
        return audit_final(run, finals[0].parent)
    directory = run / ('discovery' if stage == 'cold-start' else 'evolution/round-' + stage.split('-')[1])
    adapter = resolve(OmegaConf.load(run / 'config.json'))
    rows = []
    for task in sorted(plan.tasks_in('source')):
        path = directory / 'trials' / f'{task}.json'
        data = read(path)
        rows.append(dict(audit_checkpoint(data, adapter), usage=audit_usage(path, data)))
    result = {'integrity': 'passed', 'units': rows, 'skills': len(initial),
              'usage_complete': all(row['usage']['tokens_complete'] for row in rows)}
    if stage != 'cold-start':
        result['round'] = audit_round(run, int(stage.split('-')[1]))
    return result


def retryable_failure(exc, run, started):
    names = ('TimeoutError', 'Timeout', 'APIConnectionError', 'ConnectionError', 'RateLimitError',
             'ServiceUnavailableError', 'APIError', 'RemoteDisconnected')
    current = exc
    while current is not None:
        if type(current).__name__ in names:
            return True
        current = current.__cause__
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return False
    messages = [str(exc)]
    for path in run.rglob('errors/*.json'):
        if path.stat().st_mtime >= started:
            messages.append(str(read(path).get('error', '')))
    for path in run.rglob('evaluation_errors/*.json'):
        if path.stat().st_mtime >= started:
            messages.append(str(read(path).get('error', '')))
    return any(name in message for name in names for message in messages)


def execute_stage(root, mode, benchmark, stage, attempt):
    from skillexpand import cli
    directory = root / mode / benchmark
    started = time.time()
    try:
        cli.main(stage_args(root, mode, benchmark, stage))
        audit = audit_stage(root, mode, benchmark, stage)
        save(directory / 'audits' / f'{stage}.json', audit)
        save(directory / 'attempts' / f'{stage}-{attempt}.json',
             {'status': 'complete', 'started': started, 'finished': time.time(), 'retryable': False})
        return 0
    except Exception as exc:
        save(directory / 'attempts' / f'{stage}-{attempt}.json',
             {'status': 'failed', 'started': started, 'finished': time.time(),
              'error': f'{type(exc).__name__}: {exc}',
              'retryable': retryable_failure(exc, directory / 'run', started)})
        import traceback
        traceback.print_exc()
        return 1


def command(root, action, mode, benchmark=None, stage=None, attempt=None):
    manifest = read(root / 'manifest.json')
    cmd = [manifest['python'], '-u', str(root / 'code/run_campaign.py'), action,
           '--root', str(root), '--mode', mode]
    for key, value in (('--benchmark', benchmark), ('--stage', stage), ('--attempt', attempt)):
        if value is not None:
            cmd.extend([key, str(value)])
    return cmd


def run_job(root, mode, benchmark):
    verify(root)
    directory = root / mode / benchmark
    with locked(directory / 'job.lock'):
        state_path = directory / 'status.json'
        state = read(state_path) if state_path.exists() else {'stages': {}, 'started': time.time()}
        state.update(pid=os.getpid(), status='running')
        for stage in STAGES:
            previous = state['stages'].get(stage, {})
            if previous.get('status') == 'complete':
                audit_stage(root, mode, benchmark, stage)
                continue
            first = previous.get('attempt', 0) + 1
            for offset in range(len(RETRY_DELAYS) + 1):
                attempt = first + offset
                state.update(stage=stage, updated=time.time())
                state['stages'][stage] = {'attempt': attempt, 'status': 'running'}
                log_path = directory / 'logs' / f'{stage}-{attempt}.log'
                log_path.parent.mkdir(exist_ok=True)
                with log_path.open('ab') as log:
                    child = subprocess.Popen(command(root, '_stage', mode, benchmark, stage, attempt),
                        env=environment(root), cwd=read(root / 'manifest.json')['repo'],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
                    state['stages'][stage]['pid'] = child.pid
                    save(state_path, state)
                    rc = child.wait()
                attempt_path = directory / 'attempts' / f'{stage}-{attempt}.json'
                result = read(attempt_path) if attempt_path.exists() else {
                    'status': 'failed', 'retryable': False, 'error': f'Child exited {rc} without result'}
                complete = rc == 0 and result['status'] == 'complete'
                state['stages'][stage].update(status='complete' if complete else 'failed',
                                               returncode=rc, result=result)
                save(state_path, state)
                if complete:
                    break
                if not result.get('retryable') or offset == len(RETRY_DELAYS):
                    state.update(status='needs_attention', updated=time.time())
                    save(state_path, state)
                    return 1
                state.update(status='retry_wait', updated=time.time())
                save(state_path, state)
                time.sleep(RETRY_DELAYS[offset])
                state['status'] = 'running'
        state.update(status='complete', updated=time.time())
        save(state_path, state)
        return 0


def require_preflight(root):
    report = read(root / 'preflight/complete.json')
    if report.get('status') != 'complete' or report.get('manifest_hash') != digest(root / 'manifest.json'):
        raise ValueError('Matching complete preflight required before full launch')
    for benchmark in BENCHMARKS:
        for stage in STAGES:
            if not (root / 'preflight' / benchmark / 'audits' / f'{stage}.json').is_file():
                raise ValueError('Missing preflight stage audit')
    independent = read(root / 'preflight/independent-checks.json')
    if independent.get('status') != 'passed' or independent.get('manifest_hash') != digest(root / 'manifest.json'):
        raise ValueError('Matching independent resume/reviewer checks required')


def supervise(root, mode):
    verify(root)
    if mode == 'full':
        require_preflight(root)
    directory = root / mode
    with locked(directory / 'supervisor.lock'):
        children, logs = {}, []
        def stop(signum, frame):
            for child in children.values():
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
            raise SystemExit(128 + signum)
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            for benchmark in BENCHMARKS:
                log = (directory / f'{benchmark}.log').open('ab')
                logs.append(log)
                children[benchmark] = subprocess.Popen(command(root, '_job', mode, benchmark),
                    cwd=read(root / 'manifest.json')['repo'], env=environment(root),
                    stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
            while True:
                jobs = {}
                for benchmark, child in children.items():
                    path = directory / benchmark / 'status.json'
                    jobs[benchmark] = {'pid': child.pid, 'returncode': child.poll(),
                                       'progress': read(path) if path.exists() else {}}
                done = all(child.poll() is not None for child in children.values())
                state = {'supervisor_pid': os.getpid(), 'updated': time.time(), 'jobs': jobs,
                         'status': 'running' if not done else 'complete' if all(
                             child.returncode == 0 for child in children.values()) else 'needs_attention'}
                save(directory / 'status.json', state)
                if done:
                    break
                time.sleep(5)
            if state['status'] != 'complete':
                return 1
            # Completion is only published after both jobs and their final audits.
            save(directory / 'complete.json', {'status': 'complete', 'finished': time.time(),
                 'manifest_hash': digest(root / 'manifest.json'),
                 'results': {b: read(directory / b / 'audits/evolve-2.json') for b in BENCHMARKS}})
            return 0
        finally:
            # Only signal process groups created by this supervisor. Cleanup is
            # also necessary if a status/audit write fails while jobs are alive.
            for child in children.values():
                if child.poll() is None:
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    child.wait()
            for log in logs:
                log.close()


def start(root, mode):
    verify(root)
    if mode == 'full':
        require_preflight(root)
    health(root)
    directory = root / mode
    with locked(directory / 'launch.lock'):
        pidfile = directory / 'supervisor.pid'
        if pidfile.exists():
            pid = int(pidfile.read_text())
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise ValueError(f'Supervisor PID {pid} still exists; refusing duplicate launch')
        with (directory / 'supervisor.log').open('ab') as log:
            child = subprocess.Popen(command(root, '_supervise', mode),
                cwd=read(root / 'manifest.json')['repo'], env=environment(root),
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        pidfile.write_text(str(child.pid) + '\n')
    return {'pid': child.pid, 'mode': mode, 'status_path': str(directory / 'status.json')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'check', 'health', 'start', '_supervise', '_job', '_stage'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--inputs', type=Path)
    parser.add_argument('--mode', choices=('preflight', 'full'), default='preflight')
    parser.add_argument('--benchmark', choices=BENCHMARKS)
    parser.add_argument('--stage', choices=STAGES)
    parser.add_argument('--attempt', type=int)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == 'prepare':
        result = prepare(root, args.inputs.resolve())
        print(json.dumps({'root': str(root), 'benchmarks': result['benchmarks'], 'model': result['model']}))
    elif args.action == 'check':
        result = verify(root)
        print(json.dumps({'verified': True, 'model': result['model'], 'workers': result['workers'],
                          'benchmarks': result['benchmarks']}))
    elif args.action == 'health':
        print(json.dumps(health(root)))
    elif args.action == 'start':
        print(json.dumps(start(root, args.mode)))
    elif args.action == '_supervise':
        return supervise(root, args.mode)
    elif args.action == '_job':
        return run_job(root, args.mode, args.benchmark)
    elif args.action == '_stage':
        return execute_stage(root, args.mode, args.benchmark, args.stage, args.attempt)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
