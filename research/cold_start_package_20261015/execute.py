"""Freeze and run four autonomous mechanism slots using the existing loading runner."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import time


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def prepare(root, source):
    target = root / 'execution'
    if target.exists():
        raise ValueError('Execution freeze already exists')
    if not read(root / 'generation_review.json')['allow_mechanism_smoke']:
        raise ValueError('Generation review gate did not pass')
    for arm in ['old', 'new']:
        if read(root / 'generations' / arm / 'outcome.json')['status'] != 'valid':
            raise ValueError('Generation failed')
    target.mkdir()
    for name in ['runner.py', 'runtime.py', 'study.py', 'watch_files.py']:
        shutil.copyfile(source / name, target / name)
    shutil.copyfile(Path(__file__), root / 'execute.py')
    shutil.copytree(root / 'code', target / 'code', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copyfile(source.parents[1] / 'scripts/summarize_tb21_empirical.py', target / 'code/summarize_tb21_empirical.py')
    previous = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/loading-factorial-smoke-20261015')
    shutil.copytree(previous / 'frozen/runtime', target / 'frozen/runtime',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    task = Path(read(root / 'manifest.json')['source_task'])
    shutil.copytree(task, target / 'frozen/tasks' / task.name)
    write(target / 'frozen/task_table.json', [{'task_name': task.name,
          'instruction': (root / 'frozen/public_instruction.md').read_text()}])
    shutil.copyfile(previous / 'frozen/executor_template.json', target / 'frozen/executor_template.json')
    for origin, name in [('old', 'old'), ('new', 'evolved')]:
        path = target / 'frozen/libraries' / (name + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / 'generations' / origin / 'library.json', path)
    identities = set(read(previous / 'frozen/historical_sandbox_ids.json'))
    identities.update(read(previous / 'identity_ledger.json'))
    identities.add('historical-source-fix-git')
    write(target / 'frozen/historical_sandbox_ids.json', sorted(identities))
    schedule = [{'cell': cell, 'task': task.name, 'attempt': attempt}
                for attempt in [1, 2] for cell in ['old_autonomous', 'evolved_autonomous']]
    write(target / 'frozen/schedule.json', schedule)
    write(target / 'manifest.json', {'stage': 'mechanism_smoke', 'executor_planned': 4,
        'executor_max': 4, 'workers': 4, 'generation_max': 0,
        'harbor_python': '/data2/liyishan/tbench2-openclaw-min/.venv/bin/python',
        'autonomous_user_query_tail': '你可以读取 /opt/openclaw-skills 下的 skill。',
        'frozen_at': time.time(), 'no_feedback_adaptation': True,
        'evolved_label_means_new_initial_package': True})
    for name in ['tmp', 'cache', 'cache/pip', 'cache/uv', 'cache/pycache']:
        (target / name).mkdir(parents=True, exist_ok=True)
    paths = [p for p in target.rglob('*') if p.is_file()]
    bindings = []
    for index, path in enumerate(sorted(paths)):
        snapshot = target / 'binding_snapshots' / f'{index:04d}.bin'
        snapshot.parent.mkdir(exist_ok=True)
        shutil.copyfile(path, snapshot)
        bindings.append({'current': str(path.relative_to(target)), 'snapshot': str(snapshot.relative_to(target))})
    write(target / 'bindings.json', bindings)
    print(json.dumps({'root': str(target), 'slots': 4, 'bindings': len(bindings)}))


def execute(root):
    target = root / 'execution'
    sys.path[:0] = [str(target), str(target / 'code')]
    import runner
    runner.ROOT = target
    for key in ['TMPDIR', 'TEMP', 'TMP']:
        os.environ[key] = str(target / 'tmp')
    os.environ.update(HARBOR_E2B_FS_USER='root', TB21_PACKAGE_DNS='183.60.83.19',
        TBENCH_E2B_START_CONCURRENCY='4', TBENCH_E2B_RPC_CONCURRENCY='12',
        TB21_TOTAL_WORKER_LIMIT='250', TB21_PIPELINE_CACHE='0', TB21_TASK_VERIFIER_CACHES='{}')
    write(target / 'status.json', {'status': 'running', 'workers': 4, 'pid': os.getpid()})
    runner.worker()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--source', type=Path)
    args = parser.parse_args()
    prepare(args.root, args.source) if args.prepare else execute(args.root)
