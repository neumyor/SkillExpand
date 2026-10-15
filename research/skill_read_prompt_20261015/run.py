"""Bounded prompt-only development and independent five-slot acceptance."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import time

TAILS = {
    'v1': '开始执行任务前，请先查看上方完整skill目录，选择与你的任务最相关的skill，并使用工具读取 /opt/openclaw-skills 下对应的 SKILL.md；读取正文后再开展任务操作。至少读取一项，无需读取全库。请先单独完成读取并收到正文，再提交任务操作命令。按需读取其引用资源，任务过程中可继续加载其他skill。本次要求覆盖目录中允许不读取的可选说明。',
    'v2': '本次任务必须先完成skill加载，再执行任务操作：从上方完整目录自主选择至少一项最相关的skill，在第一轮工具调用中读取 /opt/openclaw-skills/<skill_id>/SKILL.md 的实质正文，并等待工具结果返回。该轮仅可查看目录、定位和读取skill，不要同时提交检查或修改任务文件的命令。下一轮收到正文后再开始任务。无需读全库；可按需读取引用资源或继续加载其他skill。本要求优先于目录中的可不读说明。'}
TASKS = ['fix-git', 'log-summary-date-ranges']


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def prepare(root, repository):
    if root.exists():
        raise ValueError('Existing freeze must not be overwritten')
    prior = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/cold-start-package-20261015')
    source = prior / 'execution'
    root.mkdir(parents=True)
    for name in ('run.py', 'evidence.py', 'protocol.md'):
        shutil.copyfile(repository / 'research/skill_read_prompt_20261015' / name, root / name)
    shutil.copytree(source / 'code', root / 'code', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copytree(source / 'frozen/runtime', root / 'frozen/runtime')
    for name in ('runner.py', 'study.py', 'watch_files.py'):
        shutil.copyfile(source / name, root / name)
    shutil.copyfile(repository / 'research/loading_factorial_20261015/runtime.py', root / 'runtime.py')
    shutil.copyfile(source / 'frozen/executor_template.json', root / 'frozen/executor_template.json')
    e6 = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/tb21-e6-final-library-empirical-20261010/library.json')
    library = read(e6) + read(prior / 'generations/new/library.json')
    if len(library) != 11 or len({row['skill_id'] for row in library}) != 11:
        raise ValueError('Full library topology changed')
    write(root / 'frozen/library.json', library)
    sys.path.insert(0, str(root / 'code'))
    from skillexpand import schema as S
    from skillexpand.skill_package import materialize_skill
    for row in library:
        skill = S.from_dict(S.Skill, row)
        if not skill.body.strip():
            raise ValueError('Empty Skill body')
        materialize_skill(skill, root / 'frozen/package_preflight' / skill.skill_id, package=skill.package_mode)
    tasks = Path('/data2/liyishan/tbench2-openclaw-min/sources/terminal-bench-2-1-modelbest/tasks')
    for task in TASKS:
        shutil.copytree(tasks / task, root / 'frozen/tasks' / task)
    write(root / 'frozen/task_table.json', [{'task_name': task,
        'instruction': (root / 'frozen/tasks' / task / 'instruction.md').read_text()} for task in TASKS])
    historical = set(read(source / 'frozen/historical_sandbox_ids.json')) | set(read(source / 'identity_ledger.json'))
    write(root / 'frozen/historical_sandbox_ids.json', sorted(historical))
    write(root / 'frozen/prompts.json', TAILS)
    write(root / 'frozen/plan.json', {'max_executions': 9, 'development': TASKS,
        'acceptance': ['fix-git', 'log-summary-date-ranges', 'fix-git', 'log-summary-date-ranges', 'fix-git'],
        'selection': 'v1 early stop if 2/2; else v2; most reading-order passes, tie v1; no task scores',
        'library_source': str(e6), 'git_package_source': str(prior / 'generations/new/library.json'),
        'codex_requested_model': 'gpt-6.1-sol', 'codex_requested_reasoning': 'high',
        'codex_switch_verified': False})
    bindings = []
    for index, path in enumerate(sorted(p for p in root.rglob('*') if p.is_file())):
        snapshot = root / 'binding_snapshots' / f'{index:04d}.bin'
        snapshot.parent.mkdir(exist_ok=True)
        shutil.copyfile(path, snapshot)
        bindings.append({'current': str(path.relative_to(root)), 'snapshot': str(snapshot.relative_to(root))})
    write(root / 'bindings.json', bindings)
    print(json.dumps({'prepared': True, 'skills': len(library), 'bindings': len(bindings)}))


def verify(root):
    for row in read(root / 'bindings.json'):
        if (root / row['current']).read_bytes() != (root / row['snapshot']).read_bytes():
            raise ValueError('Frozen bytes changed: ' + row['current'])


def phase(root, name):
    verify(root)
    target = root / name
    if target.exists():
        raise ValueError('Consumed phase cannot be replaced')
    if name == 'dev-v2':
        if not (root / 'dev-v1/review.json').exists() or read(root / 'dev-v1/review.json')['passes'] == 2:
            raise ValueError('v2 not admitted')
    if name == 'acceptance':
        selected = read(root / 'selection.json')['prompt']
        tasks = read(root / 'frozen/plan.json')['acceptance']
    else:
        selected, tasks = name.removeprefix('dev-'), TASKS
    target.mkdir()
    for item in ('code', 'frozen/runtime', 'frozen/tasks'):
        shutil.copytree(root / item, target / item)
    for item in ('runner.py', 'runtime.py', 'study.py', 'watch_files.py'):
        shutil.copyfile(root / item, target / item)
    for item in ('task_table.json', 'executor_template.json'):
        shutil.copyfile(root / 'frozen' / item, target / 'frozen' / item)
    write(target / 'frozen/libraries/evolved.json', read(root / 'frozen/library.json'))
    excluded = set(read(root / 'frozen/historical_sandbox_ids.json'))
    for ledger in root.glob('*/identity_ledger.json'):
        excluded.update(read(ledger))
    write(target / 'frozen/historical_sandbox_ids.json', sorted(excluded))
    rows = [{'cell': 'evolved_autonomous', 'task': task, 'attempt': index + 1} for index, task in enumerate(tasks)]
    write(target / 'frozen/schedule.json', rows)
    write(target / 'manifest.json', {'stage': name, 'executor_planned': len(rows), 'executor_max': len(rows),
        'workers': len(rows), 'harbor_python': '/data2/liyishan/tbench2-openclaw-min/.venv/bin/python',
        'prompt': selected, 'autonomous_user_query_tail': read(root / 'frozen/prompts.json')[selected]})
    for directory in ('tmp', 'cache'):
        (target / directory).mkdir()
    bindings = []
    for index, path in enumerate(sorted(p for p in target.rglob('*') if p.is_file())):
        snapshot = target / 'binding_snapshots' / f'{index:04d}.bin'
        snapshot.parent.mkdir(exist_ok=True)
        shutil.copyfile(path, snapshot)
        bindings.append({'current': str(path.relative_to(target)), 'snapshot': str(snapshot.relative_to(target))})
    write(target / 'bindings.json', bindings)
    consumed = list(root.glob('*/*/slots/*/*/*/reserved.json'))
    if len(consumed) + len(rows) > 9:
        raise ValueError('Total budget exhausted')
    sys.path[:0] = [str(target), str(target / 'code')]
    runner = load('_read_prompt_runner', target / 'runner.py')
    runner.ROOT = target
    original = runner.materialize
    def materialize(slot, skills, mode, task, transport):
        result = original(slot, skills, mode, task, transport)
        result['user_query_tail'] = read(target / 'manifest.json')['autonomous_user_query_tail']
        runner.write(slot / 'load_manifest.json', result)
        return result
    runner.materialize = materialize
    os.environ.update(HARBOR_E2B_FS_USER='root', TB21_PACKAGE_DNS='183.60.83.19',
        TBENCH_E2B_START_CONCURRENCY='5', TBENCH_E2B_RPC_CONCURRENCY='15',
        TB21_TOTAL_WORKER_LIMIT='250', TB21_PIPELINE_CACHE='0', TB21_TASK_VERIFIER_CACHES='{}')
    for key in ('TMPDIR', 'TEMP', 'TMP'):
        os.environ[key] = str(target / 'tmp')
    runner.worker()
    review(root, name)


def review(root, name):
    from evidence import evaluate
    target = root / name
    verify(root)
    verify(target)
    rows, identities = [], []
    library = read(root / 'frozen/library.json')
    for slot in sorted((target / name / 'slots').glob('*/*/*')):
        record = read(slot / 'record.json')
        result = {'slot': str(slot.relative_to(root)), 'task': record['task'], 'attempt': record['attempt'],
                  'official_status': record['status'], 'reward': record['reward'], 'reading_status': 'unresolved'}
        if record.get('trajectory_path'):
            agent = Path(record['trajectory_path']).parent
            request_file = agent / 'raw_model_requests.jsonl'
            if request_file.exists() and record['status'] == 'valid':
                requests = [json.loads(line) for line in request_file.read_text().splitlines() if line.strip()]
                proof = evaluate(read(agent / 'trajectory.json'), requests, library)
                manifest = read(slot / 'load_manifest.json')
                first = requests[0]['messages'][0]['content']
                tail = read(target / 'manifest.json')['autonomous_user_query_tail']
                expected_catalog = [{'skill_id': skill['skill_id'], 'description': skill['description']}
                                                    for skill in sorted(library, key=lambda skill: skill['skill_id'])]
                full_catalog = manifest['catalog'] == expected_catalog and json.dumps(expected_catalog, ensure_ascii=False) in first
                from skillexpand import schema as S
                from skillexpand.skill_package import entrypoint_content
                expected_files = {}
                for value in library:
                    skill = S.from_dict(S.Skill, value)
                    contents = {'SKILL.md': entrypoint_content(skill, package=skill.package_mode)}
                    if skill.package_mode:
                        contents.update(skill.resources)
                    expected_files.update({skill.skill_id + '/' + path: text for path, text in contents.items()})
                mounted_files = {str(path.relative_to(slot / 'skills')): path.read_text()
                                 for path in (slot / 'skills').rglob('*') if path.is_file()}
                mounted = mounted_files == expected_files
                paths = read(agent / 'loading_query_paths.json')
                effective = read(agent / 'effective_agent_config.json')
                parameters = all(request.get('model') == 'qwen3.6-flash-distill' and request.get('max_tokens') == 65536
                    and request.get('enable_thinking') is True and request.get('top_p') == .95
                    and request.get('top_k') == 20 and request.get('chat_template_kwargs', {}).get('preserve_thinking') is True
                    for request in requests)
                leak = any(text.strip() and text.strip() in first for text in expected_files.values())
                checks = {'exact_tail': first.endswith(tail), 'complete_catalog': full_catalog,
                    'mounted_entrypoints': mounted and paths['all_exist'] and len(paths['entrypoints']) == len(library),
                    'no_initial_body_leak': not leak, 'provider_parameters': parameters,
                    'context': effective['max_input_tokens'] == 262144 and effective['max_output_tokens'] == 65536}
                identity = record.get('sandbox_id')
                checks['fresh_identity'] = bool(identity) and identity not in read(target / 'frozen/historical_sandbox_ids.json')
                if identity:
                    identities.append(identity)
                result.update(proof, checks=checks,
                    reading_status='pass' if proof['reading_order_pass'] and all(checks.values()) else 'fail')
        rows.append(result)
    if len(identities) != len(set(identities)):
        raise ValueError('Duplicate sandbox identity')
    summary = {'phase': name, 'slots': len(rows), 'passes': sum(row['reading_status'] == 'pass' for row in rows),
               'unresolved': sum(row['reading_status'] == 'unresolved' for row in rows), 'rows': rows}
    if name == 'acceptance':
        summary['gate_passed'] = len(rows) == 5 and summary['passes'] == 5
    write(target / 'review.json', summary)
    print(json.dumps({key: value for key, value in summary.items() if key != 'rows'}), flush=True)


def select(root):
    verify(root)
    if (root / 'selection.json').exists():
        raise ValueError('Selection already frozen')
    first = read(root / 'dev-v1/review.json')
    if first['passes'] == 2:
        selected = 'v1'
    else:
        second = read(root / 'dev-v2/review.json')
        selected = 'v2' if second['passes'] > first['passes'] else 'v1'
    write(root / 'selection.json', {'prompt': selected, 'at': time.time(),
        'rule': read(root / 'frozen/plan.json')['selection'], 'task_scores_used': False,
        'text': read(root / 'frozen/prompts.json')[selected]})
    print(json.dumps(read(root / 'selection.json'), ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'dev-v1', 'dev-v2', 'select', 'acceptance', 'review'])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--repository', type=Path)
    parser.add_argument('--phase')
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args.root, args.repository)
    elif args.action == 'select':
        select(args.root)
    elif args.action == 'review':
        review(args.root, args.phase)
    else:
        phase(args.root, args.action)
