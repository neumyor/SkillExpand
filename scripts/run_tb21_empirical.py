"""Execute a frozen library through independently selected Harbor attempts."""
import argparse
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from omegaconf import OmegaConf
from skillexpand import schema as S
from skillexpand.benchmarks.terminalbench import harbor_rollout
from skillexpand.l2.loop import RunLock
from skillexpand.runtime.llm_relay import relay_from_env
from skillexpand.runtime.progressive import select_and_load
from summarize_tb21_empirical import read, write, validate_trial, execution_settings, metrics, summarize


CANARIES = ('fix-git', 'log-summary-date-ranges', 'constraints-scheduling')


def failure_class(exc):
    category = getattr(exc, 'error_class', None)
    if category:
        return category
    text = str(exc).lower()
    for tokens, category in [
        (('ratelimit',), 'provider_429'),
        (('timeout', 'timed out'), 'timeout'),
        (('connect', 'disconnect'), 'connectivity'),
        (('persistence', 'snapshot'), 'persistence'),
        (('sandbox', 'container', 'environmentsetup'), 'sandbox'),
        (('verifier',), 'verifier'),
        (('selector', 'selection'), 'selector'),
    ]:
        if any(token in text for token in tokens):
            return category
    if re.search(r'(?:http|status(?:_code)?)\s*[:=]?\s*429\b', text):
        return 'provider_429'
    return 'runtime_or_artifact'


def active_worker_reservation(runs, own):
    reservation, active = 0, []
    for status_path in runs.glob('tb21-*/status.json'):
        if status_path.parent == own:
            continue
        try:
            status = read(status_path)
        except json.JSONDecodeError:
            # Some archived recovery writers appended a literal backslash-n.
            text = status_path.read_text().strip()
            status, end = json.JSONDecoder().raw_decode(text)
            if text[end:].strip() not in ('', '\\n'):
                raise
        pid = status.get('pid')
        if status.get('status') != 'running' or not pid or not Path('/proc', str(pid)).exists():
            continue
        ledger = status_path.parent / 'continuation_ledger.json'
        if not ledger.exists():
            ledger = status_path.parent / 'recovery_ledger.json'
        config = read(ledger) if ledger.exists() else {}
        other_manifest = read(status_path.parent / 'manifest.json')
        count = status.get('workers', config.get('actual_review_workers', other_manifest.get('workers')))
        if count is None:
            other_manifest = status_path.parent / 'l2_manifest.json'
            other = read(other_manifest).get('config', {}) if other_manifest.exists() else {}
            count = max(other.get('evolve_l1_workers', 100), other.get('l2_review_workers', 100))
        reservation += int(count)
        active.append({'run': str(status_path.parent), 'pid': pid, 'reserved_workers': count})
    return reservation, active


def prepare(source, root, workers, stage='E1', baseline_source=None):
    status = read(source / 'status.json')
    assert status['status'] == 'complete'
    if stage == 'E1':
        summary = read(source / 'summary.json')
        audit = read(source / 'evolution/round-1/audit.json')
        assert summary['status'] == 'complete'
        assert audit['tasks'] == summary['train_cards'] == 89
        assert summary['invalid_batches'] == 0 and audit['round'] == 1
    else:
        audit = read(source / 'recovery_audit.json')
        assert audit['status'] == 'complete' and audit['identity_key_audit_passed']
        assert audit['reviewer_coverage_complete']
        assert baseline_source is not None
    heads = {}
    for line in (source / 'skills.jsonl').read_text().splitlines():
        skill = json.loads(line)
        old = heads.get(skill['skill_id'])
        if old is None or skill['version'] > old['version']:
            heads[skill['skill_id']] = skill
    versions = {k: f"{k}@v{s['version']}" for k, s in heads.items()}
    if stage == 'E1':
        assert len(heads) == 7 and versions == summary['skills']
        config = read(source / 'config.json')
    else:
        assert len(heads) == 1 and all(s['version'] == 0 for s in heads.values())
        scores = [json.loads(line) for line in
                  (source / 'predicted_scores.reconciled.jsonl').read_text().splitlines()]
        assert len(scores) == len({s['cache_key'] for s in scores}) == 89
        assert {s['task_id'] for s in scores} == set(range(89))
        assert {s['skill_key'] for s in scores} == set(versions.values())
        from skillexpand.evaluation.validation import ScoreCache
        skill = next(iter(heads.values()))
        assert all(s['cache_key'] == ScoreCache.make_key('terminalbench', s['panel_key'],
            s['task_id'], 'predicted:' + s['protocol_hash'], skill['body']) for s in scores)
        config = read(source / 'manifest.json')['config']
    baseline_source = baseline_source or source
    tasks = read(config['benchmark']['task_file'])
    assert len(tasks) == 89 and len({t['task_name'] for t in tasks}) == 89
    assert config['models']['l1_executor'] == 'qwen3.6-flash-distill'
    assert config['benchmark']['rollout']['llm_transport'] == 'tencent_e2b_relay'
    reserved, active = active_worker_reservation(source.parent, root)
    if not 1 <= workers <= 100 - reserved:
        raise ValueError(f'Worker cap exceeded: {workers} + reserved {reserved}')
    baseline_rows, baseline_errors, settings = [], [], []
    for task, entry in enumerate(tasks):
        card = read(baseline_source / f'discovery/results/{task}.json')
        if (card.get('initial_skill_key') is not None or card.get('selected_skill_id') is not None):
            raise ValueError('Baseline must be without Skill')
        assert [r['index'] for r in card['l1_trials']] == [1, 2, 3]
        for trial in card['l1_trials']:
            result_path = Path(trial['trajectory']) / 'result.json'
            try:
                result, reward, trajectory = validate_trial(result_path, entry['task_name'])
                agent = result.get('config', {}).get('agent', {})
                if not str(agent.get('import_path', '')).endswith(':TencentSandboxTerminus2'):
                    raise ValueError('Baseline is not bare executor')
                baseline_rows.append({'task_id': task, 'attempt_index': trial['index'],
                    'reward': reward, 'result_path': str(result_path), 'trajectory_path': trajectory})
                settings.append(execution_settings(result))
            except (ValueError, KeyError, OSError) as exc:
                baseline_errors.append({'task_id': task, 'attempt_index': trial['index'], 'error': str(exc)})
    assert len({r['result_path'] for r in baseline_rows}) == len(baseline_rows)
    root.mkdir(exist_ok=False)
    write(root / 'library.json', sorted(heads.values(), key=lambda s: s['skill_id']))
    write(root / 'tasks.json', tasks)
    write(root / 'config.json', config)
    write(root / 'baseline.json', {'source': str(baseline_source), 'records': baseline_rows,
        'metrics': metrics(baseline_rows, 89), 'settings': settings, 'rejected': baseline_errors})
    write(root / 'manifest.json', {'stage': f'{stage}_empirical', 'protocol': 'tb21-frozen-library-empirical-v1',
        'source_run': str(source), 'tasks': 89, 'attempts': 3,
        'task_names': [t['task_name'] for t in tasks], 'model': 'qwen3.6-flash-distill',
        'library_versions': versions, 'scope': '89-task closed-set',
        'selector_model': 'qwen3.6-flash-distill' if stage == 'E1' else config['models']['selector'],
        'source_kind': 'evolved_final_library' if stage == 'E1' else 'reviewed_bootstrap_v0',
        'workers': workers, 'reserved_workers': reserved, 'active_runs': active,
        'persistence': 0, 'transport': 'tencent_e2b_relay', 'canaries': list(CANARIES),
        'infrastructure_requests_per_slot_per_launch': 3})
    write(root / 'status.json', {'stage': f'{stage}_empirical', 'status': 'prepared', 'coverage': '0/267'})


class SelectorHost:
    def __init__(self, transport, evidence, model='qwen3.6-flash-distill'):
        self.transport, self.evidence, self.model = transport, evidence, model

    def llm(self, messages, **kwargs):
        request = {'model': self.model, 'enable_thinking': True,
                   'messages': [{'role': {'human': 'user', 'ai': 'assistant'}.get(m.type, m.type),
                                 'content': m.content} for m in messages]}
        response = self.transport.request(request)
        raw = response['choices'][0]['message']['content']
        write(self.evidence, {'messages': request['messages'], 'raw': raw})
        return raw


def execute_slot(cfg, tasks, skills, root, transport, task, attempt):
    slot = root / 'slots' / f'{task:02d}-{attempt}'
    existing = slot / 'record.json'
    if existing.exists() and read(existing).get('status') == 'valid':
        return read(existing)
    requests = slot / 'requests'
    requests.mkdir(parents=True, exist_ok=True)
    previous = len(list(requests.glob('*')))
    row = {'task_id': task, 'attempt_index': attempt, 'task_name': tasks[task]['task_name']}
    for retry in range(1, 4):
        request_dir = requests / f'{previous + retry:03d}'
        request_dir.mkdir(exist_ok=False)
        try:
            host = SelectorHost(transport, request_dir / 'selector.json',
                read(root / 'manifest.json').get('selector_model', 'qwen3.6-flash-distill'))
            skill, selection = select_and_load(host, tasks[task]['instruction'], skills)
            assert selection['why'].strip()
            write(request_dir / 'selection.json', selection)
            task_name = tasks[task]['task_name']
            # Harbor's skill mount normalizes this one task directory name.
            skill_directory = 'install-windows-3-11' if task_name == 'install-windows-3.11' else task_name
            skill_file = request_dir / 'skills' / skill_directory / 'SKILL.md'
            skill_file.parent.mkdir(parents=True, exist_ok=True)
            skill_file.write_text(skill.body)
            rollout, runtime = harbor_rollout(cfg, task, skill, 1, request_dir)
            write(request_dir / 'rollout.json', {'rollout': rollout, 'runtime': runtime})
            if len(rollout['trials']) != 1:
                raise ValueError('missing_or_duplicate_harbor_trial')
            trial = rollout['trials'][0]
            result, reward, trajectory = validate_trial(trial['result_path'], task_name, activation=True)
            activation = read(Path(trial['result_path']).parent / 'agent/skill_activation.json')
            if Path(activation['source_path']).resolve() != skill_file.resolve():
                raise ValueError('mounted_skill_path_mismatch')
            if skill_file.read_text() != skill.body:
                raise ValueError('mounted_skill_body_mismatch')
            row.update(status='valid', reward=reward, selection=selection, skill_file=str(skill_file),
                result_path=trial['result_path'], trajectory_path=trajectory, runtime=runtime)
            write(existing, row)
            return row
        except Exception as exc:
            message = str(exc)
            for name in ('OPENAI_API_KEY', 'E2B_API_KEY'):
                key = os.environ.get(name)
                if key:
                    message = message.replace(key, '[REDACTED]')
            row.update(status='infrastructure_error', error_class=failure_class(exc), error=message[:3000])
            write(request_dir / 'error.json', row)
            write(existing, row)
            if retry < 3:
                time.sleep(2 ** retry)
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--run-dir', required=True, type=Path)
    parser.add_argument('--workers', type=int, default=16)
    parser.add_argument('--stage', choices=('E1', 'E4'), default='E1')
    parser.add_argument('--baseline-source', type=Path)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    source, root = args.source.resolve(), args.run_dir.resolve()
    if not root.exists():
        prepare(source, root, args.workers, args.stage,
                args.baseline_source.resolve() if args.baseline_source else None)
    manifest = read(root / 'manifest.json')
    assert manifest['source_run'] == str(source) and manifest['workers'] == args.workers
    stage = manifest['stage']
    assert stage == f'{args.stage}_empirical'
    if args.prepare_only:
        print(json.dumps(manifest))
        return
    reserved, _ = active_worker_reservation(source.parent, root)
    assert args.workers + reserved <= 100
    with RunLock(root / 'run.pid'):
        relay = None
        try:
            logging.basicConfig(filename=root / 'relay.log', level=logging.INFO)
            write(root / 'status.json', {'stage': stage, 'status': 'running',
                                       'pid': os.getpid(), 'phase': 'relay_start'})
            (root / 'PID').write_text(str(os.getpid()) + '\n')
            os.environ.update(MODEL_NAME=manifest['model'], TBENCH_PERSIST_SANDBOXES='0',
                              TBENCH_TENCENT_ENV_FILE='/dev/null')
            relay = relay_from_env()
            relay.start()
            write(root / 'relay_manifest.json', {'sandbox_id': relay.transport.sandbox_id,
                'transport': 'tencent_e2b_relay', 'direct_provider_fallback': False})
            cfg = OmegaConf.create(read(root / 'config.json'))
            tasks = read(root / 'tasks.json')
            skills = [S.from_dict(S.Skill, s) for s in read(root / 'library.json')]
            canaries = [(manifest['task_names'].index(name), 1) for name in CANARIES]
            def run_batch(slots, phase):
                write(root / 'status.json', {'stage': stage, 'status': 'running',
                    'pid': os.getpid(), 'phase': phase})
                with ThreadPoolExecutor(max_workers=min(args.workers, len(slots))) as pool:
                    pending = [pool.submit(execute_slot, cfg, tasks, skills, root,
                                           relay.transport, task, attempt) for task, attempt in slots]
                    for future in as_completed(pending):
                        future.result()
                        report = summarize(root)
                        write(root / 'status.json', {'stage': stage, 'status': 'running',
                            'pid': os.getpid(), 'phase': phase,
                            'coverage': f"{report['empirical']['valid_attempts']}/267"})
            run_batch(canaries, 'canary')
            if any(read(root / 'slots' / f'{task:02d}-{attempt}/record.json')['status'] != 'valid'
                   for task, attempt in canaries):
                raise RuntimeError('Canary infrastructure failed; full panel not started')
            write(root / 'canary.json', {'passed': True, 'slots': canaries,
                                        'records_reused_in_full_panel': True})
            formal = [(t, a) for t in range(89) for a in (1, 2, 3) if (t, a) not in canaries]
            run_batch(formal, 'full_panel')
            report = summarize(root)
            audit = read(root / 'audit.json')
            write(root / 'status.json', {'stage': stage,
                'status': 'complete' if audit['complete'] else 'needs_attention',
                'coverage': f"{report['empirical']['valid_attempts']}/267", 'pid': os.getpid()})
        except Exception as exc:
            write(root / 'status.json', {'stage': stage, 'status': 'needs_attention',
                'error': str(exc), 'error_class': failure_class(exc)})
            raise
        finally:
            if relay is not None:
                relay.close()


if __name__ == '__main__':
    main()
