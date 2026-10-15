"""Second authorized prompt-only round; reuse the first round's frozen engine."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sys

TAILS = {
    'v1': ('SKILL LOADING — REQUIRED FIRST RESPONSE. The complete Skill catalog above is available. '
           'Choose the most relevant Skill yourself. In your NEXT response, the commands array MUST '
           'contain exactly ONE command: cat /opt/openclaw-skills/<chosen_skill_id>/SKILL.md followed '
           'by a newline. Replace <chosen_skill_id> with an actual ID from the catalog. '
           'Set task_complete to false. Stop that response immediately after submitting this one '
           'read command. Do not include any other command, task inspection, task data read, or task '
           'modification in this response. Wait for the Skill body to be returned in the next user '
           'message. Only THEN, in a separate subsequent response, begin task operations. '
           'Do not merely plan to read; actually submit the read command now. '
           'You need not read the whole library. Later you may load more Skills or referenced resources '
           'as needed. This requirement overrides the earlier optional-reading wording.'),
    'v2': ('FIRST TURN IS ONLY FOR READING A SKILL. You have not yet received any Skill body. '
           'Select one relevant ID from the complete catalog. Your very next JSON response must have '
           'task_complete=false and commands containing ONE element only. That element must execute '
           'cat /opt/openclaw-skills/<your_selected_id>/SKILL.md with a trailing newline. '
           'No ls, pwd, cd, task inspection, data reading, code, or other shell command may accompany '
           'this first read. End the response and wait. A cat command and a task command in the same '
           'commands array are NOT read-then-act: you would be choosing task actions before receiving '
           'the Skill. After the tool result containing the body arrives, use your next response '
           'to solve the task. Additional Skills and resources may be read later; never read all '
           'Skills by default. Ignore the earlier permission to read none: it is superseded here.')}


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def prepare(root, repository):
    if root.exists():
        raise ValueError('Existing round cannot be overwritten')
    prior = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/skill-read-prompt-20261015')
    root.mkdir(parents=True)
    for name in ('code', 'frozen'):
        shutil.copytree(prior / name, root / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in ('runner.py', 'runtime.py', 'study.py', 'watch_files.py', 'evidence.py'):
        shutil.copyfile(prior / name, root / name)
    shutil.copyfile(prior / 'run.py', root / 'engine.py')
    for name in ('run.py', 'protocol.md'):
        shutil.copyfile(repository / 'research/skill_read_prompt_r2_20261015' / name, root / name)
    shutil.copyfile(repository / 'research/skill_read_prompt_20261015/audit.py', root / 'audit.py')
    old_ids = set(read(root / 'frozen/historical_sandbox_ids.json'))
    for ledger in prior.glob('*/identity_ledger.json'):
        old_ids.update(read(ledger))
    write(root / 'frozen/historical_sandbox_ids.json', sorted(old_ids))
    write(root / 'frozen/prompts.json', TAILS)
    plan = read(root / 'frozen/plan.json')
    plan.update(round=2, prior_round=str(prior), max_executions=9,
                authorization='User requested continued prompt tuning after first round failed',
                sole_intervention='Generic first-user-query tail; all other inputs copied byte-for-byte')
    write(root / 'frozen/plan.json', plan)
    bindings = []
    for index, path in enumerate(sorted(p for p in root.rglob('*') if p.is_file())):
        snapshot = root / 'binding_snapshots' / f'{index:04d}.bin'
        snapshot.parent.mkdir(exist_ok=True)
        shutil.copyfile(path, snapshot)
        bindings.append({'current': str(path.relative_to(root)), 'snapshot': str(snapshot.relative_to(root))})
    write(root / 'bindings.json', bindings)
    print(json.dumps({'prepared': True, 'skills': len(read(root / 'frozen/library.json')), 'bindings': len(bindings)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'dev-v1', 'dev-v2', 'select', 'acceptance', 'review'])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--repository', type=Path)
    parser.add_argument('--phase')
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args.root, args.repository)
    else:
        sys.path.insert(0, str(args.root))
        spec = importlib.util.spec_from_file_location('_round_engine', args.root / 'engine.py')
        engine = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(engine)
        if args.action == 'select':
            engine.select(args.root)
        elif args.action == 'review':
            sys.path.insert(0, str(args.root / 'code'))
            engine.review(args.root, args.phase)
        else:
            engine.phase(args.root, args.action)
