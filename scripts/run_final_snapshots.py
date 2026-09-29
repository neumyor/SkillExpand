#!/usr/bin/env python3
"""Freeze and evaluate three historical Skill libraries on identical held-out tasks.

Use prepare, then preflight, then full. Each benchmark has an exclusive lock,
per-task score/usage persistence, immutable routes, and auditable snapshot outputs.
Identical library fingerprints reuse one measurement rather than resampling.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request

LABELS = ('cold-start', 'evolve-1', 'evolve-2')
WORKERS = {'searchqa': 128, 'alfworld': 32}


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def runtime(root, benchmark=None):
    repo = Path(__file__).resolve().parents[1] if not (root / 'manifest.json').exists() else Path(read(root / 'manifest.json')['repo'])
    workspace = repo.parents[2]
    local = read(workspace / 'benchmark/llm_config.json')['alfworld-eval']
    # Credentials are read at launch, never written to campaign artifacts.
    env = dict(os.environ)
    for key in ('EXPE_LLM_EXTRA_JSON', 'OPENAI_API_BASE', 'EXPE_TASK_FILE', 'EXPE_CONFIG_FILE'):
        env.pop(key, None)
    env.update(EXPE_LLM_MODEL=local['model'], EXPE_LLM_BASE_URL=local['base_url'],
        OPENAI_API_KEY=local['api_key'], EXPE_LLM_DISABLE_THINKING='1', EXPE_SHOW_ADMISSIBLE='1',
        ALFWORLD_PYTHON=str(workspace / 'benchmark/alfworld-eval/.venv/bin/python'),
        ALFWORLD_DATA=str(repo / 'data/alfworld'),
        ALFWORLD_CONFIG=str(workspace / 'benchmark/alfworld-eval/configs/textworld.yaml'),
        ALFWORLD_BENCH_SRC=str(workspace / 'benchmark/alfworld-eval/src'),
        PYTHONPATH=str(root / 'code/src' if (root / 'code/src').exists() else repo / 'src') + os.pathsep + str(workspace / 'tmp/expetoskill-runtime-py311'),
        TIKTOKEN_CACHE_DIR=str(repo / '.cache-tiktoken'), MPLCONFIGDIR=str(repo / '.mpl-cache'),
        PYTHONUNBUFFERED='1', OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1',
        TOKENIZERS_PARALLELISM='false', EXPE_LLM_TIMEOUT_SECONDS='300', EXPE_LLM_RETRIES='2',
        EXPE_ENV_TIMEOUT_SECONDS='120', EXPE_WORKER_TIMEOUT_SECONDS='3600',
        EXPE_LLM_REQUEST_INTERVAL_SECONDS='0.5',
        EXPE_LLM_GATE_FILE=str(root / f'{benchmark or "health"}-request-gate.state'))
    if benchmark:
        env['EXPE_CONFIG_FILE'] = str(root / 'inputs' / benchmark / 'config.json')
    return env


def activate(env):
    os.environ.update(env)
    for path in reversed(env['PYTHONPATH'].split(os.pathsep)):
        if path not in sys.path:
            sys.path.insert(0, path)


def select_snapshots(initial, history, round_summaries):
    """Choose recorded round-end keys, never guess versions from round numbers."""
    snapshots = {'cold-start': sorted(initial, key=lambda s: s['skill_id'])}
    by_key = {f"{s['skill_id']}@v{s['version']}": s for s in history}
    expected = {s['skill_id'] for s in initial}
    for label, summary in zip(LABELS[1:], round_summaries):
        if summary['status'] != 'complete' or set(summary['skills']) != expected:
            raise ValueError('Incomplete or inconsistent round-end library')
        selected = [by_key[key] for _, key in sorted(summary['skills'].items())]
        if {s['skill_id'] for s in selected} != expected:
            raise ValueError('Round-end key does not belong to expected Skill')
        reference = {s['skill_id']: s['description'] for s in initial}
        if any(s['description'] != reference[s['skill_id']] for s in selected):
            raise ValueError('Frozen Skill routing descriptions changed')
        snapshots[label] = selected
    return snapshots


def task_identity(table):
    """Ignore only the relocated file path embedded in ALFWorld env configs."""
    from omegaconf import OmegaConf
    rows = []
    for task in table:
        row = dict(task, env_kwargs=dict(task['env_kwargs']))
        if 'config' in row['env_kwargs']:
            config = row['env_kwargs']['config']
            config = OmegaConf.to_container(config, resolve=True) if OmegaConf.is_config(config) else dict(config)
            config['task_file'] = '<frozen-task-file>'
            row['env_kwargs']['config'] = config
        rows.append(row)
    return rows


def prepare(root, sources):
    if root.exists() and any(root.iterdir()):
        raise ValueError('prepare requires an empty directory')
    env = runtime(root)
    activate(env)
    from skillexpand import schema as S
    from skillexpand.persistence.artifacts import load_cold_start
    from skillexpand.evaluation.validation import library_fingerprint
    from skillexpand.l2.audit import audit_round
    from skillexpand.runtime.agent_factory import task_table
    repo = Path(__file__).resolve().parents[1]
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(repo / 'src', root / 'code/src', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copyfile(__file__, root / 'code/run_final_snapshots.py')
    settings = {}
    for benchmark, source in sources.items():
        cfg, plan, initial, _ = load_cold_start(source)
        audits = [audit_round(source, n) for n in (1, 2)]
        summaries = [read(source / f'evolution/round-{n}/summary.json') for n in (1, 2)]
        history = [json.loads(line) for line in (source / 'skills.jsonl').read_text().splitlines()]
        snapshots = select_snapshots([S.to_dict(s) for s in initial], history, summaries)
        dest = root / 'inputs' / benchmark
        # Preserve raw input format: task_table is a derived runtime structure,
        # containing environment constructors/configs, not a valid task-file format.
        tasks = read(cfg.benchmark.task_file)
        save(dest / 'tasks.json', tasks)
        config = read(source / 'config.json')
        if config['agent']['llm'] != env['EXPE_LLM_MODEL']:
            raise ValueError('Configured model differs from original experiment')
        config['benchmark']['task_file'] = str(dest / 'tasks.json')
        save(dest / 'config.json', config)
        from omegaconf import OmegaConf
        table_hash = S.content_hash(task_identity(task_table(OmegaConf.create(config), refresh=True)))
        if table_hash != S.content_hash(task_identity(task_table(cfg, refresh=True))):
            raise ValueError('Task-table invariant changed after freezing raw inputs')
        split = read(source / 'split.json')
        save(dest / 'split.json', split)
        # Preflight uses two SOURCE tasks, so implementation checks do not inspect Final.
        smoke_ids = sorted(plan.tasks_in(S.SPLIT_SOURCE))[:2]
        smoke_split = read(source / 'split.json')
        smoke_split['assignment'] = {str(t): ('test' if t in smoke_ids else 'train') for t in range(len(tasks))}
        save(dest / 'preflight-split.json', smoke_split)
        libs = {}
        for label, raw in snapshots.items():
            save(dest / f'{label}.json', raw)
            libs[label] = library_fingerprint([S.from_dict(S.Skill, s) for s in raw])
        settings[benchmark] = dict(source=str(source), final_tasks=len(plan.tasks_in(S.SPLIT_FINAL)),
            source_audits=audits, snapshots=libs, workers=WORKERS[benchmark], preflight_tasks=smoke_ids,
            frozen_task_identity_hash=table_hash,
            source_config_hash=digest(source / 'config.json'), source_split_hash=digest(source / 'split.json'))
    files = {str(p.relative_to(root)): digest(p) for folder in ('code', 'inputs') for p in sorted((root / folder).rglob('*')) if p.is_file()}
    save(root / 'manifest.json', dict(repo=str(repo), model=env['EXPE_LLM_MODEL'], benchmarks=settings, files=files,
        metric='Single autonomous episode; SearchQA normalized EM / ALFWorld environment success; no reflection, guidance or fewshots',
        routing='One frozen initial-description selector assignment per task, shared by all snapshots',
        unchanged_libraries='Identical fingerprints reuse one evaluation; unchanged Skill keys/bodies reuse per-task scores across libraries',
        reporting='All three snapshots and paired changes; no selection of best Final result',
        request_interval_seconds=0.5, created=time.time()))
    print(json.dumps(settings, ensure_ascii=False))


def verify(root):
    manifest = read(root / 'manifest.json')
    for relative, expected in manifest['files'].items():
        if digest(root / relative) != expected:
            raise ValueError(f'Frozen input/code changed: {relative}')
    return manifest


def health(root):
    env = runtime(root)
    payload = dict(model=env['EXPE_LLM_MODEL'], messages=[dict(role='user', content='Reply with OK.')],
                   temperature=0, max_tokens=8, enable_thinking=False)
    request = urllib.request.Request(env['EXPE_LLM_BASE_URL'].rstrip('/') + '/chat/completions',
        data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + env['OPENAI_API_KEY']})
    start = time.monotonic()
    with urllib.request.urlopen(request, timeout=60) as response:
        result = json.loads(response.read())
    content = result['choices'][0]['message'].get('content', '')
    if not content.strip():
        raise ValueError('Generation health check returned empty content')
    save(root / 'health.json', dict(model=result.get('model'), content=content, seconds=time.monotonic()-start, time=time.time()))


def evaluate(root, benchmark, mode):
    manifest = verify(root)
    env = runtime(root, benchmark)
    if env['EXPE_LLM_MODEL'] != manifest['model']:
        raise ValueError('Model changed since prepare')
    activate(env)
    from omegaconf import OmegaConf
    from skillexpand import schema as S
    from skillexpand.l1.cold_start import freeze, read_split
    from skillexpand.persistence.artifacts import code_signature, provider_signature
    from skillexpand.evaluation.routing import FrozenRoutes
    from skillexpand.evaluation.validation import FixedSkillScorer, ScoreCache
    from skillexpand.evaluation.audit import audit_final
    inputs = root / 'inputs' / benchmark
    target_root = root / mode / benchmark
    with locked(target_root / 'job.lock'):
        split = read(inputs / ('preflight-split.json' if mode == 'preflight' else 'split.json'))
        freeze(target_root / 'split.json', split)
        cfg = OmegaConf.load(inputs / 'config.json')
        plan = read_split(target_root / 'split.json')
        initial = read(inputs / 'cold-start.json')
        workers = 2 if mode == 'preflight' else manifest['benchmarks'][benchmark]['workers']
        save(target_root / 'status.json', dict(status='routing', pid=os.getpid(), updated=time.time()))
        routes = FrozenRoutes(cfg, plan, [S.from_dict(S.Skill, s) for s in initial], target_root / 'routes', S.SPLIT_FINAL, workers).run()
        shared_cache = ScoreCache(target_root / 'shared-scores.jsonl')
        results = {}
        for label in LABELS:
            fingerprint = manifest['benchmarks'][benchmark]['snapshots'][label]
            target = target_root / 'test' / fingerprint
            raw = read(inputs / f'{label}.json')
            skills = [S.from_dict(S.Skill, s) for s in raw]
            save(target_root / 'status.json', dict(status='evaluating', snapshot=label, target=str(target), pid=os.getpid(), updated=time.time()))
            freeze(target / 'library.json', raw)
            freeze(target / 'protocol.json', dict(code=code_signature(), provider=provider_signature(),
                config=read(inputs / 'config.json'), routing_reference=initial))
            if (target / 'summary.json').exists():
                audit = audit_final(target_root, target)
            else:
                scorer = FixedSkillScorer(cfg, ScoreCache(target / 'scores.jsonl'), routes, workers)
                freeze(target / 'score_protocol.json', dict(hash=scorer.protocol_hash))
                per_skill = {}
                for skill in skills:
                    panel = f'final:{routes.fingerprint}:{skill.skill_id}'
                    identity = S.content_hash(dict(protocol=scorer.protocol_hash, panel=panel, skill_id=skill.skill_id, body=skill.body))
                    for t in routes.groups[skill.skill_id]:
                        key = ScoreCache.make_key(benchmark, identity, t, S.ROLE_EVAL, skill.body)
                        hit = shared_cache.get(key)
                        if hit is not None and hit['skill_key'] == skill.key:
                            scorer.cache.put(key, dict(hit, reused_from=hit.get('reused_from', hit.get('measurement_target'))))
                    score = scorer.score(skill, routes.groups[skill.skill_id], panel)
                    for t in routes.groups[skill.skill_id]:
                        key = ScoreCache.make_key(benchmark, identity, t, S.ROLE_EVAL, skill.body)
                        shared_cache.put(key, dict(scorer.cache.get(key), measurement_target=str(target)))
                    per_skill[skill.skill_id] = dict(tasks=score.n, successes=score.successes, score=score.score)
                    save(target / 'skills' / f'{skill.skill_id}.json', per_skill[skill.skill_id])
                    print(f'{benchmark} {label} {skill.key}: {score.successes}/{score.n}; reused={score.from_cache}', flush=True)
                successes = sum(v['successes'] for v in per_skill.values())
                n = len(plan.tasks_in(S.SPLIT_FINAL))
                save(target / 'summary.json', dict(split='test', library_hash=fingerprint, routing_reference='initial_skills',
                    tasks=n, successes=successes, score=successes/n, per_skill=per_skill, routing_failures=list(routes.failed_task_ids)))
                audit = audit_final(target_root, target)
            save(target / 'audit.json', audit)
            save(target / 'usage-audit.json', audit_token_ledgers(target / 'usage'))
            results[label] = dict(target=str(target), audit=audit, **read(target / 'summary.json'))
            save(target_root / 'snapshots.json', results)
        paired = paired_results(results)
        save(target_root / 'routing-usage-audit.json',
             audit_token_ledgers(target_root / 'routes' / S.SPLIT_FINAL / 'usage'))
        save(target_root / 'paired.json', paired)
        save(target_root / 'status.json', dict(status='complete', pid=os.getpid(), snapshots=results, paired=paired, updated=time.time()))


def audit_token_ledgers(directory):
    """Recompute request totals across retries/resume; flag unknown failed-call cost."""
    aggregate = dict(started_requests=0, successful_requests=0, failed_requests=0,
                     prompt_tokens=0, completion_tokens=0, total_tokens=0)
    files = list(Path(directory).glob('*.json'))
    for path in files:
        totals = dict.fromkeys(aggregate, 0)
        pending, finished = set(), set()
        for row in path.with_suffix('.requests.jsonl').read_text().splitlines():
            event = json.loads(row)
            rid = event['run_id']
            if event['event'] == 'start':
                if not rid or rid in pending | finished:
                    raise ValueError('Duplicate usage request start')
                pending.add(rid)
                totals['started_requests'] += 1
            else:
                if event['event'] not in ('end', 'error', 'abandoned') or rid not in pending:
                    raise ValueError('Unmatched usage terminal event')
                pending.remove(rid)
                finished.add(rid)
                if event['event'] == 'end':
                    usage = (event.get('provider') or {}).get('token_usage')
                    if usage is None:
                        raise ValueError('Missing provider token usage')
                    totals['successful_requests'] += 1
                    for field in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                        totals[field] += usage[field]
                else:
                    totals['failed_requests'] += 1
        if pending:
            raise ValueError('In-flight requests remain in completed evaluation')
        reported = read(path)
        if any(reported[field] != value for field, value in totals.items()):
            raise ValueError('Persisted token totals disagree with request ledger')
        for field, value in totals.items():
            aggregate[field] += value
    return dict(integrity='passed', files=len(files), usage_complete=aggregate['failed_requests'] == 0, **aggregate)


def paired_results(results):
    outcomes = {}
    for label, result in results.items():
        scores = Path(result['target']) / 'scores.jsonl'
        rows = [json.loads(line) for line in scores.read_text().splitlines()] if scores.exists() else []
        by_task = {r['task_id']: r['success'] for r in rows}
        if len(by_task) != len(rows):
            raise ValueError('Duplicate task results')
        for task in result['routing_failures']:
            if task in by_task:
                raise ValueError('Routing failure also has execution score')
            by_task[task] = False
        if len(by_task) != result['tasks']:
            raise ValueError('Paired result coverage incomplete')
        outcomes[label] = by_task
    paired = {}
    for before, after in ((LABELS[0], LABELS[1]), (LABELS[1], LABELS[2]), (LABELS[0], LABELS[2])):
        a, b = outcomes[before], outcomes[after]
        if set(a) != set(b):
            raise ValueError('Paired task populations differ')
        cells = {'correct_to_wrong': [], 'wrong_to_correct': [], 'both_correct': [], 'both_wrong': []}
        for t in sorted(a):
            key = ('both_correct' if b[t] else 'correct_to_wrong') if a[t] else ('wrong_to_correct' if b[t] else 'both_wrong')
            cells[key].append(t)
        paired[f'{before}->{after}'] = {k: dict(count=len(v), task_ids=v) for k, v in cells.items()}
    return paired


def supervise(root, mode):
    manifest = verify(root)
    if mode == 'full':
        for benchmark in WORKERS:
            status = read(root / 'preflight' / benchmark / 'status.json')
            if status['status'] != 'complete':
                raise ValueError('Both preflights must pass before full evaluation')
    with locked(root / f'{mode}-supervisor.lock'):
        children = {}
        for benchmark in WORKERS:
            env = runtime(root, benchmark)
            log = (root / f'{mode}-{benchmark}.log').open('ab')
            child = subprocess.Popen([env['ALFWORLD_PYTHON'], '-u', str(root / 'code/run_final_snapshots.py'),
                '_job', '--root', str(root), '--benchmark', benchmark, '--mode', mode],
                env=env, cwd=manifest['repo'], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True)
            log.close()
            children[benchmark] = child
            save(root / f'{mode}-{benchmark}-pid.json', dict(pid=child.pid, started=time.time()))
        save(root / f'{mode}-supervisor-status.json', dict(status='running', pid=os.getpid(), children={b:p.pid for b,p in children.items()}))
        codes = {b: p.wait() for b, p in children.items()}
        complete = all(code == 0 for code in codes.values())
        if complete:
            complete = all(read(root / mode / b / 'status.json')['status'] == 'complete' for b in WORKERS)
        save(root / f'{mode}-supervisor-status.json', dict(status='complete' if complete else 'needs_attention',
            pid=os.getpid(), returncodes=codes, finished=time.time()))
        return 0 if complete else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'health', 'preflight', 'full', '_job'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--searchqa-source', type=Path)
    parser.add_argument('--alfworld-source', type=Path)
    parser.add_argument('--benchmark', choices=tuple(WORKERS))
    parser.add_argument('--mode', choices=('preflight', 'full'))
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == 'prepare':
        if not args.searchqa_source or not args.alfworld_source:
            parser.error('prepare requires both source directories')
        prepare(root, dict(searchqa=args.searchqa_source.resolve(), alfworld=args.alfworld_source.resolve()))
    elif args.action == 'health':
        health(root)
    elif args.action == '_job':
        if not args.benchmark or not args.mode:
            parser.error('_job requires benchmark and mode')
        try:
            evaluate(root, args.benchmark, args.mode)
        except Exception as exc:
            save(root / args.mode / args.benchmark / 'failure.json', dict(error=f'{type(exc).__name__}: {exc}', time=time.time()))
            raise
    else:
        return supervise(root, args.action)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
