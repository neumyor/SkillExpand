"""Audit the exact frozen E5 reviewer identities without issuing requests."""
import argparse
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from omegaconf import OmegaConf
from skillexpand import schema as S
from skillexpand.evaluation.validation import PredictedSkillScorer, ScoreCache
from skillexpand.l1.runner import save


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    manifest = json.loads((args.source / 'recovery_manifest.json').read_text())
    original = Path(manifest['source_run'])
    cfg = OmegaConf.load(original / 'config.json')
    base = S.from_dict(S.Skill, json.loads((original / 'initial_skills.json').read_text())[0])
    proposal = original / 'l2_proposals' / manifest['proposal_id']
    skills = [base] + [S.from_dict(S.CandidateSkill,
        json.loads((proposal / f'candidate-{i}.json').read_text())['candidate']).skill for i in range(3)]
    route = json.loads((original / 'routes/train/manifest.json').read_text())
    scorer = PredictedSkillScorer(cfg, SimpleNamespace(fingerprint=S.content_hash(route),
        groups={base.skill_id: tuple(range(89))}), None, workers=1)
    assert scorer.protocol_hash == manifest['protocol_hash']
    panel = f'val:{scorer.routes.fingerprint}:{base.skill_id}'
    assert panel == manifest['panel_key']
    expected = {}
    for skill in skills:
        for task in range(89):
            key = ScoreCache.make_key(cfg.benchmark.name, panel, task,
                'predicted:' + scorer.protocol_hash, skill.body)
            expected[key] = {'benchmark': cfg.benchmark.name, 'panel_key': panel,
                'task_id': task, 'protocol_hash': scorer.protocol_hash,
                'skill_body_hash': S.content_hash(skill.body)}
    accepted, duplicates, mismatches, conflicts = {}, [], [], []
    for line in (args.source / 'train/predicted_scores.jsonl').read_text().splitlines():
        row = json.loads(line)
        key = row['cache_key']
        identity = expected.get(key)
        if identity is None or any(row.get(k) != identity[k]
                                  for k in ('task_id', 'panel_key', 'protocol_hash')):
            mismatches.append(key)
            continue
        parsed = scorer._parse(json.dumps({k: row[k] for k in
                                         ('probability_true', 'predicted_success', 'reason')}))
        if key in accepted:
            duplicates.append(key)
            if any(accepted[key][k] != parsed[k] for k in parsed):
                conflicts.append(key)
        accepted.setdefault(key, row)
    missing = [identity for key, identity in expected.items() if key not in accepted]
    errors = json.loads((args.source / 'provider_errors.json').read_text())
    assert {e['cache_key'] for e in errors} == set(expected) - set(accepted)
    args.out.mkdir(exist_ok=False)
    save(args.out / 'cache_reconciliation.json', {'source': str(args.source),
        'expected_records': len(expected), 'reused_records': manifest['reused_records'],
        'recovered_records': len(accepted) - manifest['reused_records'],
        'valid_records': len(accepted), 'duplicates': duplicates, 'mismatches': mismatches,
        'conflicts': conflicts, 'pending_identities': missing,
        'coverage_by_body': dict(Counter(expected[k]['skill_body_hash'] for k in accepted)),
        'task65': [identity for key, identity in expected.items()
                   if identity['task_id'] == 65 and key in accepted]})
    save(args.out / 'audit.json', {'five_part_identity_checked': True,
        'no_conflicts': not conflicts, 'no_mismatches': not mismatches,
        'reviewer_coverage_complete': not missing, 'candidate_gate_executed': False,
        'provider_errors': errors, 'historical_runs_read_only': True})
    save(args.out / 'status.json', {'stage': 'E5', 'status': 'needs_attention',
        'coverage': f'{len(accepted)}/{len(expected)}', 'pending_requests': len(missing),
        'reason': 'Authorized extra request exhausted; no further automatic request',
        'source_run': str(args.source)})
    save(args.out / 'recovery_ledger.json', {'source': str(args.source),
        'original_format_corrections': 3, 'authorized_extra_requests_per_identity': 1,
        'remaining_authorized_requests_per_missing_identity': 0,
        'max_tokens_sent': False, 'candidate_committed': False})
    (args.out / 'summary.md').write_text(
        f'# E5 cache audit\n\nExact reviewer coverage: {len(accepted)}/{len(expected)}. '
        'Original corrections and the explicitly authorized extra request are exhausted. '
        'The remaining length failure is preserved; no candidate gate or commit is executed.\n')
    print(json.dumps({'valid': len(accepted), 'expected': len(expected),
                      'missing': missing, 'conflicts': conflicts, 'mismatches': mismatches}))


if __name__ == '__main__':
    main()
