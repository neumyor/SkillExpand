"""Check or publish task-Skill cards from accepted slots without executing L1."""
import argparse
import json
from pathlib import Path

from skillexpand import schema as S
from skillexpand.l2.loop import RunLock
from run_tb21_aligned import grouped_cards, collect_cards
from summarize_tb21_empirical import read


def inspect(root, *, publish=False):
    root = Path(root).resolve()
    tasks = read(root / 'tasks.json')
    skills = [S.from_dict(S.Skill, s) for s in read(root / 'initial_skills.json')]
    model = read(root / 'alignment.json')['executor_model']
    cards, _, audit = grouped_cards(root, tasks, skills, model)
    counts = {}
    for card in cards.values():
        counts.setdefault(card.task_id, set()).add(card.initial_skill_key)
    audit['tasks_with_multiple_selected_skills'] = sorted(t for t, keys in counts.items() if len(keys) > 1)
    audit['run'] = root.name
    if publish:
        with RunLock(root / 'driver.pid'):
            collect_cards(root, tasks, skills, model)
    return audit


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('run_dirs', nargs='+', type=Path)
    parser.add_argument('--publish', action='store_true', help='Publish only complete validated coverage')
    args = parser.parse_args()
    for root in args.run_dirs:
        print(json.dumps(inspect(root, publish=args.publish), ensure_ascii=False))
