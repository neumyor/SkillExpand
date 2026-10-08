"""Frozen, resumable cold-start -> evolve x2 campaign for both benchmarks.

``prepare`` copies this checkout's ``src`` into ``<root>/code/src`` and writes
``<root>/code/run_campaign.py``, a launcher that always imports that frozen
copy.  Every later action, stage and supervisor runs through the frozen
launcher, so a campaign never executes live source.  The source checkout's Git
state is recorded at preparation as provenance; drift is reported by ``check``
and is not a verification failure, because the frozen ``code/`` and ``inputs/``
digests are what define the campaign.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
import urllib.request

from skillexpand.l2 import sampled as SM
from skillexpand.persistence import io as IO
from skillexpand.reliability.errors import (
    DISPOSITIONS, AuditFailure, Category, FrozenProtocolChanged, Halt, InvalidInput, LedgerCorrupt,
    RunLocked, classify,
)
from skillexpand.reliability.policies import STAGE_ATTEMPTS_BY_CATEGORY, repair_policy, stage_policy
from skillexpand.reliability.retry import call_with_repair
from skillexpand.reliability.units import exit_now

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
#: Written to ``<root>/code/run_campaign.py`` and covered by the frozen digests.
FROZEN_LAUNCHER = '''#!/usr/bin/env python3
"""Frozen campaign launcher: always runs this campaign's own code/src."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from skillexpand.campaign import main

if __name__ == '__main__':
    raise SystemExit(main())
'''


def configured_runtime():
    names = ('EXPE_LLM_MODEL', 'EXPE_LLM_BASE_URL', 'ALFWORLD_PYTHON', 'EXPE_CAMPAIGN_OVERLAY',
             'ALFWORLD_DATA', 'ALFWORLD_CONFIG', 'ALFWORLD_BENCH_SRC')
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise InvalidInput('Source scripts/env.sh and set: ' + ', '.join(missing))
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
            raise InvalidInput(f'Configured {key} must be a {kind}: {paths[key]}')
    return {'model': values['EXPE_LLM_MODEL'], 'llm_base_url': values['EXPE_LLM_BASE_URL'],
            **{key: str(path) for key, path in paths.items()}}


def read(path):
    return json.loads(Path(path).read_text())


ROLES = ('l1_executor', 'cold_start', 'l2_planner', 'l2_editor', 'l2_reviewer',
         'l2_verifier', 'selector')


def save(path, value):
    IO.save(path, value, indent=2)


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
                    raise LedgerCorrupt(f'Invalid usage ledger start: {path}')
                pending.add(run_id)
                totals['started_requests'] += 1
                continue
            if event not in ('end', 'error', 'abandoned') or run_id not in pending:
                raise LedgerCorrupt(f'Invalid usage ledger terminal event: {path}')
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


def git_identity(repo):
    """Record the exact source revision, including an intentional dirty tree."""
    repo = str(repo)
    commit = subprocess.check_output(
        ['git', '-C', repo, 'rev-parse', 'HEAD'], text=True
    ).strip()
    status = subprocess.check_output(
        ['git', '-C', repo, 'status', '--porcelain=v1', '--untracked-files=all'],
        text=True,
    )
    return {
        'commit': commit,
        'dirty': bool(status),
        'status_hash': hashlib.sha256(status.encode()).hexdigest(),
    }


def source_drift(manifest):
    """Compare the live source checkout with the Git state recorded at preparation.

    Provenance only: stages execute the frozen ``code/`` copy, never the checkout.
    """
    try:
        current = git_identity(Path(manifest['repo']))
    except (OSError, subprocess.CalledProcessError):
        return {'available': False}
    return {'available': True, 'changed': current != manifest['git'],
            'recorded': manifest['git'], 'current': current}


def locked(path):
    return IO.exclusive_lock(path)


def source_checkout():
    """The checkout whose ``src`` is frozen by ``prepare``; never a frozen copy."""
    repo = Path(__file__).resolve().parents[2]
    if not (repo / 'pyproject.toml').is_file() or not (repo / 'src' / 'skillexpand').is_dir():
        raise InvalidInput('prepare must run from a source checkout, not a frozen campaign copy')
    return repo


def validate_inputs(tasks, split):
    assignment = split.get('assignment', split)
    if set(map(int, assignment)) != set(range(len(tasks))):
        raise InvalidInput('Split must cover each task exactly once')
    counts = Counter(assignment.values())
    if set(counts) != {'train', 'val', 'test'}:
        raise InvalidInput('All three disjoint splits are required')
    return dict(counts)


def prepare(root, inputs, skill_edit_mode='structured', acceptance_mode='predicted', models=None,
            autonomous_attempts=4, supervised_attempts=1,
            predicted_review_scope='val', candidate_count=1,
            single_candidate=False, reviewer_update_mode=None,
            reviewer_feedback_size=0, **sampled_options):
    if skill_edit_mode not in ('rewrite', 'structured'):
        raise InvalidInput('Unknown Skill edit mode')
    if acceptance_mode not in ('predicted', 'empirical', 'jev', 'sampled'):
        raise InvalidInput('Unknown acceptance mode')
    if reviewer_update_mode is None:
        reviewer_update_mode = SM.default_reviewer_update_mode(acceptance_mode)
    unknown = set(sampled_options) - set(SM.DEFAULTS)
    if unknown:
        raise InvalidInput(f'Unknown campaign option(s): {sorted(unknown)}')
    sampled_options = {**SM.DEFAULTS, **sampled_options}
    SM.validate_options(dict(sampled_options, acceptance_mode=acceptance_mode,
                             skill_edit_mode=skill_edit_mode,
                             reviewer_update_mode=reviewer_update_mode))
    if predicted_review_scope not in ('val', 'train_cards'):
        raise InvalidInput('Unknown predicted review scope')
    if candidate_count < 1 or (single_candidate and candidate_count != 1):
        raise InvalidInput('single-candidate campaigns require candidate_count=1')
    if reviewer_update_mode not in ('none', 'summary', 'rules'):
        raise InvalidInput('Unknown reviewer update mode')
    if reviewer_feedback_size < 0:
        raise InvalidInput('reviewer_feedback_size must be nonnegative')
    repo = source_checkout()
    source_git = git_identity(repo)
    runtime = configured_runtime()
    if root.exists() and any(root.iterdir()):
        raise InvalidInput('Prepare requires a new campaign directory')
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(repo / 'src', root / 'code' / 'src',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.egg-info'))
    (root / 'code' / 'run_campaign.py').write_text(FROZEN_LAUNCHER)
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
            raise InvalidInput('Preflight needs four train tasks')
        save(root / 'inputs' / f'{benchmark}-preflight-tasks.json', [tasks[t] for t in selected])
        save(root / 'inputs' / f'{benchmark}-preflight-split.json',
             {'assignment': {'0': 'train', '1': 'train', '2': 'val', '3': 'test'}})
        details[benchmark] = {'counts': counts, 'preflight_original_task_ids': selected,
                              'original_tasks': str(task_path), 'original_split': str(split_path)}
    files = {str(p.relative_to(root)): digest(p) for folder in ('code', 'inputs')
             for p in sorted((root / folder).rglob('*')) if p.is_file()}
    default_model = runtime.pop('model')
    role_models = {role: (models or {}).get(role) or default_model for role in ROLES}
    manifest = {
        'schema': 1, 'repo': str(repo),
        'source_commit': source_commit(repo), 'git': source_git, **runtime,
        'models': role_models,
        'concurrency': CONCURRENCY, 'evolve_rounds': 2,
        'autonomous_attempts': autonomous_attempts, 'supervised_attempts': supervised_attempts,
        'batch_size': 50, 'candidate_count': candidate_count,
        'single_candidate': single_candidate,
        'reviewer_update_mode': reviewer_update_mode,
        'reviewer_feedback_size': reviewer_feedback_size,
        **sampled_options,
        'skill_edit_mode': skill_edit_mode, 'acceptance_mode': acceptance_mode,
        'predicted_review_scope': predicted_review_scope,
        'benchmarks': details,
        'request_interval_seconds': REQUEST_INTERVAL_SECONDS,
        'files': files, 'created': time.time(),
        'metric': 'Train-task first-autonomous-attempt success at cold start, evolve-1, and evolve-2; paired by task.',
        'comparison': 'Report each benchmark independently using fixed cold-start, Evolve-1, Evolve-2, and held-out test stages; no best-round selection and no test-based Skill selection.',
        'timeouts': {'request': 300, 'environment': 120, 'worker_progress': 3600},
    }
    save(root / 'manifest.json', manifest)
    return verify(root)


def verify(root):
    manifest = read(root / 'manifest.json')
    for relative, expected in manifest['files'].items():
        if digest(root / relative) != expected:
            raise FrozenProtocolChanged(f'Frozen campaign file changed: {relative}')
    models = manifest['models']
    candidate_count = int(manifest['candidate_count'])
    single_candidate = manifest['single_candidate']
    reviewer_update_mode = manifest['reviewer_update_mode']
    feedback_size = int(manifest['reviewer_feedback_size'])
    if (candidate_count < 1 or not isinstance(single_candidate, bool) or
            (single_candidate and candidate_count != 1) or
            reviewer_update_mode not in ('none', 'summary', 'rules') or
            feedback_size < 0):
        raise FrozenProtocolChanged('Unexpected Reviewer coevolution protocol')
    if set(models) != set(ROLES) or not all(models.values()):
        raise InvalidInput('Every model role must be configured')
    if (manifest['concurrency'] != CONCURRENCY or
            manifest['evolve_rounds'] != 2 or
            manifest['skill_edit_mode'] not in ('rewrite', 'structured') or
            manifest['acceptance_mode'] not in ('predicted', 'empirical', 'jev',
                                                'sampled') or
            manifest['predicted_review_scope'] not in ('val', 'train_cards') or
            int(manifest['autonomous_attempts']) < 1 or
            int(manifest['supervised_attempts']) < 0 or
            manifest['request_interval_seconds'] != REQUEST_INTERVAL_SECONDS):
        raise FrozenProtocolChanged('Unexpected campaign protocol')
    try:
        SM.validate_options({key: manifest[key] for key in (
            *SM.DEFAULTS, 'acceptance_mode', 'skill_edit_mode', 'reviewer_update_mode')})
    except (InvalidInput, KeyError) as exc:
        raise FrozenProtocolChanged(f'Unexpected sampled acceptance protocol: {exc}') from exc
    for key, kind in (('python', 'file'), ('overlay', 'dir'), ('alfworld_data', 'dir'),
                      ('alfworld_config', 'file'), ('alfworld_bench_src', 'dir')):
        path = Path(manifest[key])
        if not (path.is_file() if kind == 'file' else path.is_dir()):
            raise InvalidInput(f'Missing configured {key}: {path}')
    for benchmark, settings in manifest['benchmarks'].items():
        counts = validate_inputs(read(root / 'inputs' / f'{benchmark}-tasks.json'),
                                 read(root / 'inputs' / f'{benchmark}-split.json'))
        if counts != settings['counts']:
            raise FrozenProtocolChanged('Frozen split counts changed')
    return manifest


def environment(root):
    manifest = read(root / 'manifest.json')
    env = dict(os.environ)
    missing = [name for name in ('EXPE_LLM_BASE_URL', 'OPENAI_API_KEY') if not env.get(name)]
    if missing:
        raise InvalidInput('Source scripts/env.sh and set: ' + ', '.join(missing))
    if env['EXPE_LLM_BASE_URL'] != manifest['llm_base_url']:
        raise InvalidInput('LLM endpoint differs from the frozen campaign manifest')
    for key in ('EXPE_CONFIG_FILE', 'EXPE_TASK_FILE', 'EXPE_LLM_EXTRA_JSON', 'OPENAI_API_BASE'):
        env.pop(key, None)
    timeout = manifest['timeouts']
    models = manifest['models']
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
        EXPE_LLM_TIMEOUT_SECONDS=str(timeout['request']),
        EXPE_LLM_GATE_FILE=str(root / 'request-gate.state'),
        EXPE_LLM_REQUEST_INTERVAL_SECONDS=str(manifest['request_interval_seconds']),
        EXPE_ENV_TIMEOUT_SECONDS=str(timeout['environment']), EXPE_WORKER_TIMEOUT_SECONDS=str(timeout['worker_progress']))
    return env


def health(root):
    env = environment(root)
    # The probe uses the same per-model thinking policy as worker processes.
    os.environ.update(env)
    models = read(root / 'manifest.json')['models']
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
            raise InvalidInput(
                f'Health request served model {reported_model!r}, expected {model!r} '
                f'or a known canonical alias')
        content = value['choices'][0]['message'].get('content', '')
        if not content.strip():
            raise InvalidInput(f'Health request returned empty content for model {model}')
        probes.append({'requested_model': model, 'reported_model': reported_model,
                       'thinking': payload.get('enable_thinking'),
                       'seconds': round(time.monotonic() - started, 3), 'content': content})
    result = {'models': models, 'probes': probes, 'time': time.time()}
    save(root / 'health.json', result)
    return result


def stage_args(root, mode, benchmark, stage):
    manifest = read(root / 'manifest.json')
    concurrency = manifest['concurrency'][benchmark]
    suffix = '-preflight' if mode == 'preflight' else ''
    task_path = root / 'inputs' / f'{benchmark}{suffix}-tasks.json'
    run = root / mode / benchmark / 'run'
    if (run / 'cold_start_complete.json').exists():
        inherited_path = Path(read(run / 'config.json')['benchmark']['task_file'])
        if digest(inherited_path) != digest(task_path):
            raise FrozenProtocolChanged('Imported cold-start task data differs from campaign inputs')
        task_path = inherited_path
    args = ['--benchmark', benchmark, '--run-dir', str(root / mode / benchmark / 'run'),
        '--task-file', str(task_path),
        '--split-file', str(root / 'inputs' / f'{benchmark}{suffix}-split.json'),
        '--cold-start-workers', str(concurrency['cold_start_workers']),
        '--family-discovery-workers', str(concurrency['family_discovery_workers']),
        '--evolve-l1-workers', str(concurrency['evolve_l1_workers']),
        '--l2-review-workers', str(concurrency['l2_review_workers']),
        '--test-workers', str(concurrency['test_workers']),
        '--autonomous-attempts', str(manifest['autonomous_attempts']),
        '--supervised-attempts', str(manifest['supervised_attempts']),
        '--batch-size', str(manifest['batch_size']),
        '--candidate-count', str(manifest['candidate_count']), '--resume']
    args += ['--skill-edit-mode', manifest['skill_edit_mode']]
    args += ['--acceptance-mode', manifest['acceptance_mode']]
    if manifest['acceptance_mode'] == 'predicted':
        args += ['--predicted-review-scope', manifest['predicted_review_scope']]
    if manifest['single_candidate']:
        args += ['--single-candidate']
    args += ['--reviewer-update-mode', manifest['reviewer_update_mode']]
    if manifest['reviewer_feedback_size']:
        args += ['--reviewer-feedback-size', str(manifest['reviewer_feedback_size'])]
    if manifest['acceptance_mode'] == SM.MODE:
        for key in SM.DEFAULTS:
            args += ['--' + key.replace('_', '-'), str(manifest[key])]
    models = manifest['models']
    for flag, key in (('--l1-model', 'l1_executor'), ('--cold-start-model', 'cold_start'),
                      ('--l2-planner-model', 'l2_planner'), ('--l2-editor-model', 'l2_editor'),
                      ('--l2-reviewer-model', 'l2_reviewer'),
                      ('--l2-verifier-model', 'l2_verifier'),
                      ('--selector-model', 'selector')):
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
    from skillexpand.l1.artifacts import load_cold_start
    from skillexpand.l2.audit import audit_round
    from skillexpand.evaluation.audit import audit_test
    run = root / mode / benchmark / 'run'
    cfg, plan, initial, _ = load_cold_start(run)
    requested_models = read(root / 'manifest.json')['models']
    configured_models = {
        role: str(cfg.models[role])
        for role in requested_models
    }
    if configured_models != requested_models:
        raise AuditFailure('Actual role models differ from requested models')
    if stage == 'test':
        summary = read(run / 'summary.json')
        if summary['latest_evolution_round'] != 2:
            raise AuditFailure('Test preceded the second evolve round')
        tests = list((run / 'test').glob('*/summary.json'))
        if len(tests) != 1:
            raise AuditFailure('Test must contain exactly one evaluated library')
        result = audit_test(run, tests[0].parent)
        result['usage_ledger'] = audit_usage_ledgers(run)
        if not result['usage_ledger']['tokens_complete']:
            raise AuditFailure('Test usage ledger is incomplete')
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
        raise AuditFailure(f'{stage} usage audit is incomplete')
    if stage != 'cold-start':
        result['round'] = audit_round(run, int(stage.split('-')[1]))
    return result


def failure_summary(exc):
    """Category and disposition of a stage failure, from the exception itself."""
    category = classify(exc)
    disposition = DISPOSITIONS[category]
    return {'category': category.value, 'retryable': disposition.retryable,
            'halt': disposition.halt.value}


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
    except Exception as exc:  # noqa: BLE001 - the attempt record carries the disposition
        summary = failure_summary(exc)
        save(directory / 'attempts' / f'{stage}-{attempt}.json',
             {'status': 'failed', 'started': started, 'finished': time.time(),
              'error': f'{type(exc).__name__}: {exc}', **summary})
        import traceback
        traceback.print_exc()
        if not summary['retryable']:
            exit_now(1)
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
        policy = stage_policy()
        max_attempts = policy.attempts or 0
        state_path = directory / 'status.json'
        state = read(state_path) if state_path.exists() else {'stages': {}, 'started': time.time()}
        state.pop('halt', None)
        state.update(pid=os.getpid(), status='running',
                     recovery={'stage_max_attempts': max_attempts,
                               'reviewer_attempts': repair_policy('reviewer.predicted_val').attempts})
        for stage in STAGES:
            previous = state['stages'].get(stage, {})
            if previous.get('status') == 'complete':
                audit_stage(root, mode, benchmark, stage)
                continue
            first = previous.get('attempt', 0) + 1
            offset = 0
            # Consecutive same-category failures survive job restarts.
            streak = previous.get('category_streak', 0)
            last_category = previous.get('last_category')
            while True:
                attempt = first + offset
                state.update(stage=stage, updated=time.time())
                state.pop('next_retry_at', None)
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
                # A stage killed by a signal (OOM, operator) is an infrastructure
                # interruption; any other exit without its record is a defect.
                result = read(attempt_path) if attempt_path.exists() else (
                    {'status': 'failed', 'error': f'Child killed by signal {-rc}',
                     'category': Category.INFRASTRUCTURE.value, 'retryable': True,
                     'halt': Halt.AFTER_STAGE.value} if rc < 0 else
                    {'status': 'failed', 'error': f'Child exited {rc} without result',
                     'category': Category.BUG.value, 'retryable': False, 'halt': Halt.ALL.value})
                complete = rc == 0 and result['status'] == 'complete'
                state['stages'][stage].update(status='complete' if complete else 'failed',
                                               returncode=rc, result=result)
                save(state_path, state)
                if complete:
                    break
                category = result.get('category')
                streak = streak + 1 if category == last_category else 1
                last_category = category
                state['stages'][stage].update(category_streak=streak, last_category=category)
                limit = STAGE_ATTEMPTS_BY_CATEGORY.get(category)
                if limit is not None and streak >= limit:
                    result = dict(result, retryable=False,
                                  error=f"{result.get('error')} ({category} failures in "
                                        f"{streak} consecutive attempts)")
                if not result.get('retryable') or (max_attempts and offset + 1 >= max_attempts):
                    state.update(status='needs_attention', updated=time.time(),
                                 failure_category=result.get('category'),
                                 halt=result.get('halt', Halt.STAGE.value))
                    save(state_path, state)
                    return 1
                delay = policy.delay(offset)
                state.update(status='retry_wait', updated=time.time(),
                             next_retry_at=time.time() + delay)
                save(state_path, state)
                time.sleep(delay)
                offset += 1
                state['status'] = 'running'
        state.update(status='complete', updated=time.time())
        save(state_path, state)
        return 0


def require_preflight(root):
    report = read(root / 'preflight/complete.json')
    if report.get('status') != 'complete' or report.get('manifest_hash') != digest(root / 'manifest.json'):
        raise InvalidInput('Matching complete preflight required before full launch')
    for benchmark in BENCHMARKS:
        for stage in STAGES:
            if not (root / 'preflight' / benchmark / 'audits' / f'{stage}.json').is_file():
                raise InvalidInput('Missing preflight stage audit')
    independent = read(root / 'preflight/independent-checks.json')
    if independent.get('status') != 'passed' or independent.get('manifest_hash') != digest(root / 'manifest.json'):
        raise InvalidInput('Matching independent resume/reviewer checks required')


def evidence_hashes(run):
    """Byte digests of every unit result and request ledger a replay must not touch."""
    paths = set()
    for pattern in ('discovery/results/*.json', 'discovery/trials/*.json',
                    'evolution/round-*/cards/*.json', 'evolution/round-*/trials/*.json',
                    'l2_proposals/*/*.json', 'l2_patterns/*.json', '**/*.requests.jsonl'):
        paths.update(run.glob(pattern))
    return {str(path.relative_to(run)): digest(path) for path in sorted(paths)}


#: A replay attempt number that no supervisor schedules.
REPLAY_ATTEMPT = 999


PROBE_CORRECTION = (
    'Return exactly {"candidates":[{"id":"C1",'
    '"evidence_ids":[],"rule_ids":[],"reason":"...",'
    '"old_outcome":"failure","new_outcome":"unknown"}, ...]} '
    'with C1, C2, C3 once each. The top-level keys must NOT be '
    'C1/C2/C3. Use only supplied IDs; do not use placeholders such '
    'as etc. Copy card.current_observed_outcome into old_outcome '
    'when known; otherwise infer CURRENT separately or use unknown. Do not '
    'return label or effect; the program derives the relative effect. '
    'For a directional outcome difference provide at least one card '
    'evidence ID and one changed rule ID.')


#: The probe edit is generic on purpose: it must apply to any initial Skill of
#: either benchmark, and whether it helps is not what the probe checks.
PROBE_EDIT = {'op': 'add', 'section': 'completion_checks', 'target_id': None,
              'text': 'Before the final action, check that the latest observation supports it.'}
PROBE_CLAIM = ('the executor is about to take its final action',
               'it re-reads the latest observation before acting')


def sampled_acceptance_probe(root, benchmark, run, cfg, manifest):
    """Run one real sampled validation on the preflight tasks and audit it.

    The preflight split has a single val task, so its evolve stages can only
    end in ``insufficient_sample`` or an empty-panel hold: the PPI bound, the
    two-arm execution over a real sample and the verifier on real trajectories
    would otherwise first run in the full campaign.  This probe drives exactly
    that path on all preflight tasks -- every one drawn from full train, so no
    val or test task is exposed -- with the frozen sample ceiling, confidence
    and verifier switch, then replays the result through the sampled audit.
    Whether the generic probe edit is accepted is deliberately not asserted.
    """
    from types import SimpleNamespace
    from skillexpand import schema as S
    from skillexpand import structured_skill as SS
    from skillexpand.evaluation import validation as VA
    from skillexpand.evaluation.claim_check import TrajectoryVerifier
    from skillexpand.evaluation.delta_review import PairedDeltaReviewer
    from skillexpand.evaluation.sampled_validation import SampledDeltaValidator
    from skillexpand.l2.sampled_audit import audit_candidate
    from skillexpand.runtime import agent_factory as F

    skills = [S.from_dict(S.Skill, item) for item in read(run / 'initial_skills.json')]
    base = sorted(skills, key=lambda item: item.skill_id)[0]
    candidate = S.Skill(base.skill_id, base.family_id, base.version + 1, base.name,
                        base.description,
                        SS.render(SS.apply_edit(SS.from_legacy(base.body), PROBE_EDIT)))
    claim = S.Claim(*PROBE_CLAIM)
    panel = tuple(range(len(F.task_table(cfg))))
    routes = SimpleNamespace(groups={base.skill_id: panel}, fingerprint='preflight-probe')
    directory = root / 'preflight' / 'sampled-probe' / benchmark

    def host(role):
        return lambda task_id, usage_path: F.build_reasoning_host(cfg, usage_path, role=role)

    verifier = None
    if manifest['claim_verification'] == 'on':
        verifier = TrajectoryVerifier(cfg, VA.ScoreCache(directory / 'verifications.jsonl'),
                                      workers=1, host_factory=host('l2_verifier'))
    validator = SampledDeltaValidator(
        cfg, routes,
        PairedDeltaReviewer(cfg, routes, VA.ScoreCache(directory / 'delta_predictions.jsonl'),
                            workers=1, host_factory=host('l2_reviewer')),
        VA.FixedSkillScorer(cfg, VA.ScoreCache(directory / 'sampled_scores.jsonl'), routes, 1),
        sample_size=manifest['acceptance_sample_size'],
        confidence=manifest['acceptance_confidence'], verifier=verifier)
    # Fixed-Skill workers are spawned processes that read the run's frozen
    # config and task file from the environment, as a CLI stage would set them.
    saved = {key: os.environ.get(key) for key in ('EXPE_CONFIG_FILE', 'EXPE_TASK_FILE')}
    os.environ.update(EXPE_CONFIG_FILE=str(run / 'config.json'),
                      EXPE_TASK_FILE=str(cfg.benchmark.task_file))
    try:
        panel_key = f'probe:{benchmark}'
        result = validator.validate(base, candidate, claim, panel_key,
                                    sample_key=f'sampled:{panel_key}:probe')
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    recorded = result.to_dict()
    try:
        audit_candidate(recorded, panel, validator.sample_size, validator.confidence,
                        claim.claim_id, base.key)
    except AuditFailure as exc:
        raise AuditFailure(f'{benchmark}: sampled probe failed its audit: {exc}') from exc
    decision = recorded['decision']
    if decision['lower'] is None:
        raise AuditFailure(f'{benchmark}: sampled probe produced no PPI bound '
                           f'({decision["reason"]})')
    report = {'panel': list(panel), 'sample': list(result.sample_task_ids),
              'executions': result.executions, 'reviewer_requests': result.reviewer_requests,
              'decision': decision,
              'verifier_categories': sorted(row['verification']['category']
                                            for row in recorded['rows'] if row['verification'])}
    save(directory / 'report.json', report)
    return report


def independent_checks(root):
    """Resume, reviewer-format and frozen-plan checks required before a full launch.

    Runs inside the frozen launcher, so the reviewer probe uses this campaign's code.
    """
    from omegaconf import OmegaConf
    from skillexpand import schema as S
    from skillexpand.runtime import agent_factory as F
    from skillexpand.l2 import card_review as CR

    manifest = verify(root)
    os.environ.update(environment(root))
    checks = {}
    for benchmark in BENCHMARKS:
        run = root / 'preflight' / benchmark / 'run'
        before = evidence_hashes(run)
        with (root / 'preflight' / f'{benchmark}-resume-check.log').open('ab') as log:
            subprocess.run(command(root, '_stage', 'preflight', benchmark, 'evolve-2', REPLAY_ATTEMPT),
                           cwd=manifest['repo'], env=environment(root), stdout=log,
                           stderr=subprocess.STDOUT, check=True)
        if evidence_hashes(run) != before:
            raise AuditFailure(f'{benchmark}: replay changed evidence or issued requests')

        cfg = OmegaConf.load(run / 'config.json')
        base = S.from_dict(S.Skill, read(run / 'initial_skills.json')[0])
        exp = S.from_dict(S.TaskExperience, read(run / 'evolution/round-2/cards/0.json'))
        candidates = [
            {'id': 'C1', 'body': base.body + '\nCheck the observation before acting.'},
            {'id': 'C2', 'body': base.body + '\nKeep the final response concise.'},
            {'id': 'C3', 'body': base.body + '\nUse the observed feedback to check progress.'},
        ]
        card = CR.card_payload([exp])[0]
        host = F.build_reasoning_host(
            cfg, root / 'preflight/reviewer-probe' / f'{benchmark}.usage.json', role='l2_reviewer')
        reviewer = CR.CardReviewer(host)
        probe = root / 'preflight/reviewer-probe' / f'{benchmark}-response-v2.json'

        def probe_request(attempt, previous):
            path = probe if attempt == 0 else probe.with_name(probe.stem + f'-repair-{attempt}.json')
            if path.exists():
                return read(path)['raw'], False
            if previous is None:
                raw = reviewer.review(base, candidates, card)
                save(path, {'raw': raw})
                return raw, True
            correction = {'error': str(previous.error), 'instruction': PROBE_CORRECTION}
            raw = reviewer.review(base, candidates, card, correction=correction)
            save(path, {'raw': raw, 'correction': correction})
            return raw, True

        probed = call_with_repair(
            repair_policy('campaign.reviewer_probe'), probe_request,
            lambda raw: CR.parse_card_review(raw, base, candidates, card))
        judgments, corrected = probed.value, probed.attempts > 1
        if len(judgments) != len(candidates):
            raise AuditFailure(f'{benchmark}: reviewer omitted a candidate')

        output = subprocess.check_output(
            [manifest['python'], '-m', 'skillexpand',
             *stage_args(root, 'full', benchmark, 'cold-start'), '--show-plan'],
            cwd=manifest['repo'], env=environment(root), text=True)
        plan = json.loads(output)
        if plan['counts'] != manifest['benchmarks'][benchmark]['counts']:
            raise AuditFailure(f'{benchmark}: full split differs from registered inputs')
        if (root / 'full' / benchmark / 'run').exists():
            raise AuditFailure(f'{benchmark}: read-only plan created a run directory')
        checks[benchmark] = {'resume_unchanged': True, 'evidence_files': len(before),
                             'reviewer_candidates': len(judgments),
                             'reviewer_format_correction': corrected,
                             'full_plan': plan}
        # The sampled protocol never calls the card reviewer probed above, and
        # the preflight split cannot reach its acceptance path; drive it here.
        if manifest['acceptance_mode'] == 'sampled':
            checks[benchmark]['sampled_acceptance'] = sampled_acceptance_probe(
                root, benchmark, run, cfg, manifest)
    save(root / 'preflight/independent-checks.json',
         {'status': 'passed', 'manifest_hash': digest(root / 'manifest.json'), 'checks': checks})
    return checks


def run_test(root, benchmark):
    """Run and audit held-out test under the frozen campaign environment."""
    if benchmark not in BENCHMARKS:
        raise InvalidInput(f'Unknown benchmark: {benchmark}')
    manifest = verify(root)
    complete = root / 'full' / 'complete.json'
    if not complete.exists():
        raise InvalidInput('Full evolution must complete before held-out test')
    record = read(complete)
    if record.get('status') != 'complete' or record.get('manifest_hash') != digest(root / 'manifest.json'):
        raise InvalidInput('Matching complete full campaign required before held-out test')
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
                # A programming error in one job halts the whole campaign: sibling
                # jobs would otherwise keep running the same defective code.
                halted_by = sorted(b for b, job in jobs.items() if job['returncode'] is not None
                                   and job['progress'].get('halt') == Halt.ALL.value)
                if halted_by:
                    state.update(status='needs_attention', halted_by=halted_by)
                save(directory / 'status.json', state)
                if done or halted_by:
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
                raise RunLocked(f'Supervisor PID {pid} still exists; refusing duplicate launch')
        with (directory / 'supervisor.log').open('ab') as log:
            child = subprocess.Popen(command(root, '_supervise', mode),
                cwd=read(root / 'manifest.json')['repo'], env=environment(root),
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        pidfile.write_text(str(child.pid) + '\n')
    return {'pid': child.pid, 'mode': mode, 'status_path': str(directory / 'status.json')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'check', 'independent-check', 'health', 'test',
                                           'start', '_supervise', '_job', '_stage'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--inputs', type=Path)
    parser.add_argument('--mode', choices=('preflight', 'full'), default='preflight')
    parser.add_argument('--benchmark', choices=BENCHMARKS)
    parser.add_argument('--stage', choices=STAGES)
    parser.add_argument('--attempt', type=int)
    parser.add_argument('--skill-edit-mode', choices=('rewrite', 'structured'), default='structured',
                        help='Skill editing mode frozen when preparing a campaign')
    parser.add_argument('--acceptance-mode',
                        choices=('predicted', 'empirical', 'jev', 'sampled'),
                        default='predicted',
                        help='Skill acceptance mode frozen when preparing a campaign')
    SM.add_arguments(parser)
    parser.add_argument('--predicted-review-scope', choices=('val', 'train_cards'), default='val',
                        help='Evidence scope for predicted acceptance')
    parser.add_argument('--candidate-count', type=int, default=1)
    parser.add_argument('--single-candidate', action='store_true')
    parser.add_argument('--reviewer-update-mode', choices=('none', 'summary', 'rules'),
                        help='Default: rules, or none under sampled acceptance')
    parser.add_argument('--reviewer-feedback-size', type=int, default=0)
    parser.add_argument('--autonomous-attempts', type=int, default=4)
    parser.add_argument('--supervised-attempts', type=int, default=1)
    parser.add_argument('--l1-model')
    parser.add_argument('--cold-start-model')
    parser.add_argument('--l2-planner-model')
    parser.add_argument('--l2-editor-model')
    parser.add_argument('--l2-reviewer-model')
    parser.add_argument('--l2-verifier-model')
    parser.add_argument('--selector-model')
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == 'prepare':
        models = {'l1_executor': args.l1_model, 'cold_start': args.cold_start_model,
                  'l2_planner': args.l2_planner_model, 'l2_editor': args.l2_editor_model,
                  'l2_reviewer': args.l2_reviewer_model,
                  'l2_verifier': args.l2_verifier_model, 'selector': args.selector_model}
        result = prepare(root, args.inputs.resolve(), args.skill_edit_mode, args.acceptance_mode,
                         models=models, autonomous_attempts=args.autonomous_attempts,
                         supervised_attempts=args.supervised_attempts,
                         predicted_review_scope=args.predicted_review_scope,
                         candidate_count=args.candidate_count,
                         single_candidate=args.single_candidate,
                         reviewer_update_mode=args.reviewer_update_mode,
                         reviewer_feedback_size=args.reviewer_feedback_size,
                         **SM.options_from(args))
        print(json.dumps({'root': str(root), 'benchmarks': result['benchmarks'], 'models': result['models']}))
    elif args.action == 'check':
        result = verify(root)
        print(json.dumps({'verified': True, 'models': result['models'],
                          'concurrency': result['concurrency'],
                          'benchmarks': result['benchmarks'],
                          'source_drift': source_drift(result)}))
    elif args.action == 'independent-check':
        print(json.dumps(independent_checks(root)))
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
