"""Freeze one isolated smoke or formal run using the prior audited library pair."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import time

from study import CELLS, schedule


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def prepare(root, prior, source, stage, smoke_root=None):
    root, prior, source = root.resolve(), prior.resolve(), source.resolve()
    if (root / 'manifest.json').exists():
        raise ValueError('Already frozen; do not overwrite')
    audit = read(prior / 'completion_audit.json')
    if not audit['passed'] or not audit['protocol_complete']:
        raise ValueError('Prior library pair must be audited and complete')
    if stage == 'formal':
        if smoke_root is None:
            raise ValueError('Formal requires completed development smoke')
        state = read(smoke_root / 'status.json')
        smoke_rows = list((smoke_root / 'smoke').glob('slots/*/*/*/record.json'))
        if state['status'] != 'finished_pending_audit' or len(smoke_rows) != 8:
            raise ValueError('Eight declared smoke slots must finish first')
        if list((root / 'formal').glob('**/reserved.json')):
            raise ValueError('Formal already started')
    root.mkdir(parents=True, exist_ok=True)
    for name in ('runner.py', 'runtime.py', 'study.py', 'watch_files.py', 'audit.py', 'protocol.md'):
        shutil.copy2(source / name, root / name)
    shutil.copytree(prior / 'frozen/tasks', root / 'frozen/tasks')
    shutil.copytree(prior / 'frozen/runtime', root / 'frozen/runtime',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.pytest_cache'))
    harbor_package = Path('/data2/liyishan/tbench2-openclaw-min/.venv/lib/python3.12/site-packages/harbor')
    shutil.copytree(harbor_package, root / 'frozen/runtime/framework/harbor',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copytree(source.parents[1] / 'src/skillexpand', root / 'code/skillexpand',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    # The experiment never imports the evolver; native routing and rendering are frozen.
    shutil.copy2(source.parents[1] / 'scripts/summarize_tb21_empirical.py', root / 'code/summarize_tb21_empirical.py')
    old, evolved = read(prior / 'frozen/seed_library.json'), read(prior / 'final/library.json')
    topology = lambda library: [(s['skill_id'], s['family_id'], s['name'], s['description']) for s in library]
    if topology(old) != topology(evolved):
        raise ValueError('Frozen library topology differs')
    for name, origin in [('old', prior / 'frozen/seed_library.json'),
                         ('evolved', prior / 'final/library.json')]:
        target = root / f'frozen/libraries/{name}.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origin, target)
    for name in ('task_table.json', 'panel.json', 'selector_settings.json', 'executor_template.json'):
        shutil.copy2(prior / 'frozen' / name, root / 'frozen' / name)
    table = read(root / 'frozen/task_table.json')
    plan = schedule([t['task_name'] for t in table]) if stage == 'formal' else [
        {'cell': cell, 'task': task, 'attempt': 1}
        for task in ['kv-store-grpc', 'headless-terminal'] for cell in CELLS]
    write(root / 'frozen/schedule.json', plan)
    identities = set(read(prior / 'frozen/historical_sandbox_ids.json'))
    identities.update(read(prior / 'identity_ledger.json'))
    if smoke_root:
        identities.update(read(smoke_root / 'identity_ledger.json'))
        write(root / 'frozen/smoke_records.json', [read(p) for p in sorted(smoke_rows)])
    write(root / 'frozen/historical_sandbox_ids.json', sorted(identities))
    for name in ('tmp', 'cache', 'cache/uv', 'cache/pip', 'cache/pycache'):
        (root / name).mkdir(parents=True, exist_ok=True)
    probe = root / 'tmp/write-probe'
    with probe.open('wb') as stream:
        import os
        stream.write(b'loading experiment writable\n')
        stream.flush()
        os.fsync(stream.fileno())
    probe.unlink()
    if shutil.disk_usage(root).free < 20 * 1024**3:
        raise OSError('Less than 20GiB free')
    manifest = {'format': 'loading-factorial-v1', 'stage': stage, 'remote_root': str(root),
        'frozen_at': time.time(), 'executor_planned': len(plan), 'executor_max': 84 if stage == 'formal' else 8,
        'total_authorized_max': 92, 'generation_max': 0, 'workers': 12 if stage == 'formal' else 8,
        'harbor_python': '/data2/liyishan/tbench2-openclaw-min/.venv/bin/python',
        'source_prior_root': str(prior), 'smoke_root': str(smoke_root) if smoke_root else None,
        'codex_requested_model': 'gpt-6.1-sol', 'codex_requested_reasoning': 'high',
        'codex_root_switch_verified': False, 'final_feedback_used': False,
        'provider': 'https://llm-center.modelbest.co/v1', 'executor_context': 262144,
        'cell_definitions': CELLS, 'formal_results_viewed_before_freeze': False}
    if stage == 'formal':
        manifest['protocol_revision'] = 'autonomous-user-query-tail-v2'
        manifest['autonomous_user_query_tail'] = '你可以读取 /opt/openclaw-skills 下的 skill。'
        manifest['pre_freeze_user_revision'] = {
            'formal_slots_consumed': 0, 'existing_smoke_preserved': True,
            'existing_smoke_validates_new_hint': False, 'additional_smoke': 0}
    write(root / 'manifest.json', manifest)
    paths = [p for p in root.glob('*.py')] + [root / 'protocol.md', root / 'manifest.json']
    paths += [p for p in (root / 'code').rglob('*') if p.is_file()]
    paths += [p for p in (root / 'frozen').rglob('*') if p.is_file()]
    bindings = []
    for index, path in enumerate(sorted(set(paths))):
        target = root / 'binding_snapshots' / f'{index:04d}.bin'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        bindings.append({'current': str(path.relative_to(root)), 'snapshot': str(target.relative_to(root))})
    write(root / 'bindings.json', bindings)
    print(json.dumps({'root': str(root), 'stage': stage, 'bindings': len(bindings), 'planned': len(plan)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--prior', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--stage', choices=['smoke', 'formal'], required=True)
    parser.add_argument('--smoke-root', type=Path)
    args = parser.parse_args()
    prepare(args.root, args.prior, args.source, args.stage, args.smoke_root)
