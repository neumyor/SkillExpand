"""Recompute test metrics from frozen routes and per-task execution records."""
import argparse
import json
from pathlib import Path

from skillexpand import schema as S
from skillexpand.evaluation.selector import parse_selection
from skillexpand.evaluation.validation import ScoreCache, library_fingerprint
from skillexpand.persistence.io import require
from skillexpand.persistence.io import save


def audit_test(root, target):
    root, target = Path(root), Path(target)
    read = lambda path: json.loads(path.read_text())
    summary = read(target / 'summary.json')
    skills = [S.from_dict(S.Skill, s) for s in read(target / 'library.json')]
    require(summary['library_hash'] == library_fingerprint(skills), 'test library hash mismatch')
    split = read(root / 'split.json')
    ids = sorted(int(t) for t, part in split['assignment'].items() if part == S.SPLIT_TEST)
    route_dir = root / 'routes' / S.SPLIT_TEST
    manifest = read(route_dir / 'manifest.json')
    routes = read(route_dir / 'complete.json')
    require(routes['fingerprint'] == S.content_hash(manifest), 'route fingerprint mismatch')
    require(sorted(map(int, manifest['tasks'])) == ids, 'test routing coverage mismatch')
    reference = read(target / 'protocol.json')['routing_reference']
    require(manifest['descriptions'] ==
            [{'skill_id': s['skill_id'], 'description': s['description']}
             for s in sorted(reference, key=lambda s: s['skill_id'])], 'routing reference changed')
    groups = {s.skill_id: [] for s in skills}
    failed = []
    require({p.name for p in (route_dir / 'tasks').glob('*.json')} ==
            {f'{t}.json' for t in ids}, 'unexpected/missing test routes')
    for t in ids:
        record = read(route_dir / 'tasks' / f'{t}.json')
        require(record['task_id'] == t and not record['failure'], 'invalid route record')
        choice = record['selection']
        selected = parse_selection(choice.get('raw', ''), groups)
        require(choice['ok'] == (selected is not None), 'route differs from raw selector output')
        if selected is None:
            failed.append(t)
        else:
            require(selected == choice['skill_id'], 'selected Skill mismatch')
            groups[selected].append(t)
    require(groups == routes['groups'] and failed == routes['failed_task_ids'], 'route group mismatch')
    score_protocol = read(target / 'score_protocol.json')['hash']
    score_path = target / 'scores.jsonl'
    records = [json.loads(line) for line in score_path.read_text().splitlines()] if score_path.exists() else []
    scores = {r['cache_key']: r for r in records}
    require(len(scores) == len(records), 'duplicate test score records')
    used, per_skill = set(), {}
    for skill in skills:
        panel = f'test:{routes["fingerprint"]}:{skill.skill_id}'
        identity = S.content_hash({'protocol': score_protocol, 'panel': panel,
                                  'skill_id': skill.skill_id, 'body': skill.body})
        successes = 0
        for t in groups[skill.skill_id]:
            key = ScoreCache.make_key(split['benchmark'], identity, t, S.ROLE_EVAL, skill.body)
            require(key in scores, f'missing test result: {t}')
            result = scores[key]
            require(result['task_id'] == t and result['skill_id'] == skill.skill_id and
                    result['skill_key'] == skill.key and result['protocol_hash'] == score_protocol and
                    result['panel_key'] == panel and not result['failure'], 'test result identity mismatch')
            require(type(result['success']) is bool, 'test outcome is not boolean')
            observations = [e for e in result['events'] if 'observation' in e]
            require(result['success'] == (observations[-1]['environment']['success'] if observations else False),
                    'test outcome differs from environment result')
            successes += int(result['success'])
            used.add(key)
        n = len(groups[skill.skill_id])
        per_skill[skill.skill_id] = dict(tasks=n, successes=successes, score=successes/n if n else None)
    require(used == set(scores), 'unexpected test score records')
    successes = sum(s['successes'] for s in per_skill.values())
    require(summary['split'] == S.SPLIT_TEST and summary['tasks'] == len(ids) and
            summary['successes'] == successes and summary['score'] == (successes/len(ids) if ids else None)
            and summary['routing_failures'] == failed and summary['per_skill'] == per_skill,
            'test summary differs from unit results')
    return dict(integrity='passed', tasks=len(ids), measured=len(used), routing_failures=len(failed),
                successes=successes, score=summary['score'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--test-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = audit_test(args.run_dir, args.test_dir)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        save(args.output, dict(integrity='failed', error=str(exc)))
        raise SystemExit(1)
    save(args.output, result)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
