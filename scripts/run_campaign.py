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
CONCURRENCY = {
    'searchqa': {
        'cold_start_workers': 128,
        'family_discovery_workers': 128,
        'evolve_l1_workers': 128,
        'l2_review_workers': 8,
        'test_workers': 128,
    },
    'alfworld': {
        'cold_start_workers': 32,
        'family_discovery_workers': 32,
        'evolve_l1_workers': 32,
        'l2_review_workers': 8,
        'test_workers': 32,
    },
}
REQUEST_INTERVAL_SECONDS = 0.5
RETRY_DELAYS = (15, 60, 180)


def configured_runtime():
    names = ('EXPE_LLM_MODEL', 'EXPE_LLM_BASE_URL', 'ALFWORLD_PYTHON', 'EXPE_CAMPAIGN_OVERLAY',
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
    return {'model': values['EXPE_LLM_MODEL'], 'llm_base_url': values['EXPE_LLM_BASE_URL'],
            **{key: str(path) for key, path in paths.items()}}


def read(path):
    return json.loads(Path(path).read_text())


def role_models(manifest):
    """Return the frozen role map, accepting pre-role-map manifests for reads."""
    current = manifest.get('models') or {}
    fallback = manifest.get('model', '')
    return {role: current.get(role) or fallback for role in (
        'l1_executor', 'cold_start', 'l2_planner', 'l2_editor',
        'l2_reviewer', 'selector')}


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


def audit_usage_ledgers(run):
    """Read-only audit for every persistent request ledger below a run."""
    totals = {
        'files': 0, 'started_requests': 0, 'successful_requests': 0,
        'failed_requests': 0, 'prompt_tokens': 0, 'completion_tokens': 0,
        'total_tokens': 0, 'tokens_complete': True,
    }
    for path in sorted(Path(run).glob('**/*.requests.jsonl')):
        totals['files'] += 1
        pending = set()
        finished = set()
        for raw in path.read_text().splitlines():
            row = json.loads(raw)
            run_id = row.get('run_id')
            event = row.get('event')
            if event == 'start':
                if not run_id or run_id in pending or run_id in finished:
                    raise ValueError(f'Invalid usage ledger start: {path}')
                pending.add(run_id)
                totals['started_requests'] += 1
                continue
            if event not in ('end', 'error', 'abandoned') or run_id not in pending:
                raise ValueError(f'Invalid usage ledger terminal event: {path}')
            pending.remove(run_id)
            finished.add(run_id)
            if event == 'end':
                usage = (row.get('provider') or {}).get('token_usage')
                if not usage:
                    totals['tokens_complete'] = False
                    continue
                totals['successful_requests'] += 1
                for field in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                    totals[field] += int(usage.get(field, 0))
            else:
                totals['failed_requests'] += 1
        if pending:
            totals['tokens_complete'] = False
    return totals


def source_commit(repo):
    """Record the source revision without making preparation depend on Git."""
    try:
        return subprocess.check_output(
            ['git', '-C', str(repo), 'rev-parse', 'HEAD'],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


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
    if set(counts) != {'train', 'val', 'test'}:
        raise ValueError('All three disjoint splits are required')
    return dict(counts)


def prepare(root, inputs, skill_edit_mode='rewrite', acceptance_mode='predicted', models=None,
            autonomous_attempts=4, supervised_attempts=1,
            predicted_review_scope='val'):
    if skill_edit_mode not in ('rewrite', 'structured'):
        raise ValueError('Unknown Skill edit mode')
    if acceptance_mode not in ('predicted', 'empirical', 'jev'):
        raise ValueError('Unknown acceptance mode')
    if predicted_review_scope not in ('val', 'train_cards'):
        raise ValueError('Unknown predicted review scope')
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
        # All smoke tasks come from full TRAIN. No test question is exposed
        # during implementation checks or used to select the smoke sample.
        selected = sorted(int(t) for t, part in assignment.items() if part == 'train')[:4]
        if len(selected) < 4:
            raise ValueError('Preflight needs four train tasks')
        save(root / 'inputs' / f'{benchmark}-preflight-tasks.json', [tasks[t] for t in selected])
        save(root / 'inputs' / f'{benchmark}-preflight-split.json',
             {'assignment': {'0': 'train', '1': 'train', '2': 'val', '3': 'test'}})
        details[benchmark] = {'counts': counts, 'preflight_original_task_ids': selected,
                              'original_tasks': str(task_path), 'original_split': str(split_path)}
    files = {str(p.relative_to(root)): digest(p) for folder in ('code', 'inputs')
             for p in sorted((root / folder).rglob('*')) if p.is_file()}
    role_models = {'l1_executor': runtime['model'], 'cold_start': runtime['model'],
                   'l2_planner': runtime['model'], 'l2_editor': runtime['model'],
                   'l2_reviewer': runtime['model'], 'selector': runtime['model']}
    if models:
        role_models.update({k: v for k, v in models.items() if v})
    manifest = {
        'schema': 1, 'repo': str(repo), 'source_commit': source_commit(repo), **runtime,
        'models': role_models,
        'concurrency': CONCURRENCY, 'evolve_rounds': 2,
        'autonomous_attempts': autonomous_attempts, 'supervised_attempts': supervised_attempts,
        'batch_size': 50, 'candidate_count': 3,
        'skill_edit_mode': skill_edit_mode, 'acceptance_mode': acceptance_mode,
        'predicted_review_scope': predicted_review_scope,
        'benchmarks': details,
        'request_interval_seconds': REQUEST_INTERVAL_SECONDS,
        'files': files, 'created': time.time(),
        'metric': 'Train-task first-autonomous-attempt success at cold start, evolve-1, and evolve-2; paired by task.',
        'comparison': 'Report each benchmark independently using fixed cold-start, Evolve-1, Evolve-2, and held-out test stages; no best-round selection and no test-based Skill selection.',
        'timeouts': {'request': 300, 'request_retries': 2, 'environment': 120, 'worker_progress': 3600},
    }
    # Keep the legacy single-model field truthful for tools that predate the
    # role map.  The executor is the model used by the environment-facing agent.
    manifest['model'] = role_models['l1_executor']
    save(root / 'manifest.json', manifest)
    return verify(root)


def verify(root):
    manifest = read(root / 'manifest.json')
    for relative, expected in manifest['files'].items():
        if digest(root / relative) != expected:
            raise ValueError(f'Frozen campaign file changed: {relative}')
    models = role_models(manifest)
    if (not manifest.get('model') and not all(models.values())):
        raise ValueError('No LLM model configured')
    if (not all(models.values()) or manifest['concurrency'] != CONCURRENCY or
            manifest['evolve_rounds'] != 2 or
            manifest.get('skill_edit_mode', 'rewrite') not in ('rewrite', 'structured') or
            manifest.get('acceptance_mode', 'predicted') not in ('predicted', 'empirical', 'jev') or
            manifest.get('predicted_review_scope', 'val') not in ('val', 'train_cards') or
            int(manifest.get('autonomous_attempts', 4)) < 1 or
            int(manifest.get('supervised_attempts', 1)) < 0 or
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
    expected_base_url = manifest.get('llm_base_url')
    if expected_base_url and env['EXPE_LLM_BASE_URL'] != expected_base_url:
        raise ValueError('LLM endpoint differs from the frozen campaign manifest')
    for key in ('EXPE_CONFIG_FILE', 'EXPE_TASK_FILE', 'EXPE_LLM_EXTRA_JSON', 'OPENAI_API_BASE'):
        env.pop(key, None)
    timeout = manifest['timeouts']
    models = role_models(manifest)
    thinking_models = sorted(model for model in set(models.values())
                             if model.startswith('glm-5.3'))
    env.update(EXPE_LLM_MODEL=models['l1_executor'], EXPE_LLM_DISABLE_THINKING='1',
        EXPE_LLM_ENABLE_THINKING_MODELS=','.join(thinking_models), EXPE_SHOW_ADMISSIBLE='1',
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
    # The probe uses the same per-model thinking policy as worker processes.
    os.environ.update(env)
    models = role_models(read(root / 'manifest.json'))
    probes = []
    for model in sorted(set(models.values())):
        from skillexpand.runtime.models.llm import (
            accepted_reported_model_names, thinking_request_kwargs,
        )
        payload = {'model': model, 'messages': [{'role': 'user', 'content': 'Reply with OK.'}],
                   'max_tokens': 8, 'temperature': 0}
        payload.update(thinking_request_kwargs(model))
        request = urllib.request.Request(env['EXPE_LLM_BASE_URL'].rstrip('/') + '/chat/completions',
            data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json',
                'Authorization': 'Bearer ' + env['OPENAI_API_KEY']})
        started = time.monotonic()
        with urllib.request.urlopen(request, timeout=60) as response:
            value = json.loads(response.read())
        reported_model = value.get('model')
        if reported_model not in accepted_reported_model_names(model):
            raise ValueError(
                f'Health request served model {reported_model!r}, expected {model!r} '
                f'or a known canonical alias')
        content = value['choices'][0]['message'].get('content', '')
        if not content.strip():
            raise ValueError(f'Health request returned empty content for model {model}')
        probes.append({'requested_model': model, 'reported_model': reported_model,
                       'thinking': payload.get('enable_thinking'),
                       'seconds': round(time.monotonic() - started, 3), 'content': content})
    result = {'models': models, 'probes': probes, 'time': time.time()}
    # Keep the old single-model fields for small tooling that reads health.json.
    if len(probes) == 1:
        result.update(probes[0])
    save(root / 'health.json', result)
    return result


def stage_args(root, mode, benchmark, stage):
    manifest = read(root / 'manifest.json')
    concurrency = manifest['concurrency'][benchmark]
    suffix = '-preflight' if mode == 'preflight' else ''
    args = ['--benchmark', benchmark, '--run-dir', str(root / mode / benchmark / 'run'),
        '--task-file', str(root / 'inputs' / f'{benchmark}{suffix}-tasks.json'),
        '--split-file', str(root / 'inputs' / f'{benchmark}{suffix}-split.json'),
        '--cold-start-workers', str(concurrency['cold_start_workers']),
        '--family-discovery-workers', str(concurrency['family_discovery_workers']),
        '--evolve-l1-workers', str(concurrency['evolve_l1_workers']),
        '--l2-review-workers', str(concurrency['l2_review_workers']),
        '--test-workers', str(concurrency['test_workers']),
        '--autonomous-attempts', str(manifest['autonomous_attempts']),
        '--supervised-attempts', str(manifest.get('supervised_attempts', 1)),
        '--batch-size', str(manifest['batch_size']),
        '--candidate-count', str(manifest['candidate_count']), '--resume']
    args += ['--skill-edit-mode', manifest.get('skill_edit_mode', 'rewrite')]
    args += ['--acceptance-mode', manifest.get('acceptance_mode', 'predicted')]
    args += ['--predicted-review-scope', manifest.get('predicted_review_scope', 'val')]
    models = role_models(manifest)
    for flag, key in (('--l1-model', 'l1_executor'), ('--cold-start-model', 'cold_start'),
                      ('--l2-planner-model', 'l2_planner'), ('--l2-editor-model', 'l2_editor'),
                      ('--l2-reviewer-model', 'l2_reviewer'), ('--selector-model', 'selector')):
        args += [flag, models[key]]
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
    from skillexpand.evaluation.audit import audit_test
    run = root / mode / benchmark / 'run'
    cfg, plan, initial, _ = load_cold_start(run)
    requested_models = role_models(read(root / 'manifest.json'))
    configured_models = {
        role: str(cfg.get('models', {}).get(role) or cfg.agent.llm)
        for role in requested_models
    }
    if configured_models != requested_models:
        raise ValueError('Actual role models differ from requested models')
    if stage == 'test':
        summary = read(run / 'summary.json')
        if summary['latest_evolution_round'] != 2:
            raise ValueError('Test preceded the second evolve round')
        tests = list((run / 'test').glob('*/summary.json'))
        if len(tests) != 1:
            raise ValueError('Test must contain exactly one evaluated library')
        result = audit_test(run, tests[0].parent)
        result['usage_ledger'] = audit_usage_ledgers(run)
        if not result['usage_ledger']['tokens_complete']:
            raise ValueError('Test usage ledger is incomplete')
        return result
    directory = run / ('discovery' if stage == 'cold-start' else 'evolution/round-' + stage.split('-')[1])
    adapter = resolve(OmegaConf.load(run / 'config.json'))
    rows = []
    for task in sorted(plan.tasks_in('train')):
        path = directory / 'trials' / f'{task}.json'
        data = read(path)
        rows.append(dict(audit_checkpoint(data, adapter), usage=audit_usage(path, data)))
    result = {'integrity': 'passed', 'units': rows, 'skills': len(initial),
              # A transient provider failure is recorded in the usage report,
              # but a successful retry still makes the checkpoint auditable.
              'usage_complete': all(row['usage'].get('audit_complete', False)
                                    for row in rows)}
    result['usage_ledger'] = audit_usage_ledgers(run)
    result['usage_complete'] = result['usage_complete'] and result['usage_ledger']['tokens_complete']
    if not result['usage_complete']:
        raise ValueError(f'{stage} usage audit is incomplete')
    if stage != 'cold-start':
        result['round'] = audit_round(run, int(stage.split('-')[1]))
    return result


def retryable_failure(exc, run, started):
    names = ('TimeoutError', 'Timeout', 'APIConnectionError', 'ConnectionError', 'RateLimitError',
             'ServiceUnavailableError', 'APIError', 'RemoteDisconnected',
             'Incomplete predicted validation',
             # The predicted scorer persists successful task records and emits
             # this task-local aggregate when one or more tasks remain missing.
             # Retrying the stage therefore resumes from its cache rather than
             # discarding the completed tasks.
             'PredictedValidationError')
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


def run_test(root, benchmark):
    """Run and audit held-out test under the frozen campaign environment."""
    if benchmark not in BENCHMARKS:
        raise ValueError(f'Unknown benchmark: {benchmark}')
    manifest = verify(root)
    complete = root / 'full' / 'complete.json'
    if not complete.exists():
        raise ValueError('Full evolution must complete before held-out test')
    record = read(complete)
    if record.get('status') != 'complete' or record.get('manifest_hash') != digest(root / 'manifest.json'):
        raise ValueError('Matching complete full campaign required before held-out test')
    health(root)
    subprocess.run(
        [manifest['python'], '-m', 'skillexpand',
         *stage_args(root, 'full', benchmark, 'test')],
        cwd=manifest['repo'], env=environment(root), check=True,
    )
    audit = audit_stage(root, 'full', benchmark, 'test')
    save(root / 'full' / benchmark / 'audits' / 'test.json', audit)
    return audit


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
    parser.add_argument('action', choices=('prepare', 'check', 'health', 'test', 'start', '_supervise', '_job', '_stage'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--inputs', type=Path)
    parser.add_argument('--mode', choices=('preflight', 'full'), default='preflight')
    parser.add_argument('--benchmark', choices=BENCHMARKS)
    parser.add_argument('--stage', choices=STAGES)
    parser.add_argument('--attempt', type=int)
    parser.add_argument('--skill-edit-mode', choices=('rewrite', 'structured'), default='rewrite',
                        help='Skill editing mode frozen when preparing a campaign')
    parser.add_argument('--acceptance-mode', choices=('predicted', 'empirical', 'jev'), default='predicted',
                        help='Skill acceptance mode frozen when preparing a campaign')
    parser.add_argument('--predicted-review-scope', choices=('val', 'train_cards'), default='val',
                        help='Evidence scope for predicted acceptance')
    parser.add_argument('--autonomous-attempts', type=int, default=4)
    parser.add_argument('--supervised-attempts', type=int, default=1)
    parser.add_argument('--l1-model')
    parser.add_argument('--cold-start-model')
    parser.add_argument('--l2-planner-model')
    parser.add_argument('--l2-editor-model')
    parser.add_argument('--l2-reviewer-model')
    parser.add_argument('--selector-model')
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == 'prepare':
        models = {'l1_executor': args.l1_model, 'cold_start': args.cold_start_model,
                  'l2_planner': args.l2_planner_model, 'l2_editor': args.l2_editor_model,
                  'l2_reviewer': args.l2_reviewer_model, 'selector': args.selector_model}
        result = prepare(root, args.inputs.resolve(), args.skill_edit_mode, args.acceptance_mode,
                         models=models, autonomous_attempts=args.autonomous_attempts,
                         supervised_attempts=args.supervised_attempts,
                         predicted_review_scope=args.predicted_review_scope)
        print(json.dumps({'root': str(root), 'benchmarks': result['benchmarks'], 'model': result['model']}))
    elif args.action == 'check':
        result = verify(root)
        print(json.dumps({'verified': True, 'model': result['model'],
                          'concurrency': result['concurrency'],
                          'benchmarks': result['benchmarks']}))
    elif args.action == 'health':
        print(json.dumps(health(root)))
    elif args.action == 'test':
        if not args.benchmark:
            parser.error('test requires --benchmark')
        print(json.dumps(run_test(root, args.benchmark)))
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
