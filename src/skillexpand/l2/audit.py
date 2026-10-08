"""Offline integrity checks for one Skill-aware L1 -> L2 evolution round."""
import argparse
import json
from pathlib import Path

from skillexpand import schema as S
from skillexpand.l1.runner import save
from skillexpand.persistence import store as ST
from skillexpand.l2.editor import SkillEditor
from skillexpand.l2.update import SkillPatchRunner
from skillexpand.l2.card_review import OUTCOMES as CR_OUTCOMES
from skillexpand.l2.patterns import validate_cache
from skillexpand.l1.audit import audit_checkpoint
from skillexpand.l1.adapters import resolve
from omegaconf import OmegaConf
from skillexpand.l2 import structured_skill as SS


def require(condition, message):
    if not condition:
        raise ValueError(message)


def audit_batch(root, batch, base, cards):
    """Replay cached decisions without any model, environment, or file writes."""
    root = Path(root)
    records = ST.read_jsonl(root / 'meta_skills.jsonl', repair_tail=False)
    require(len(records) == 1 and records[0]['version'] == 0, 'L3 must stay frozen')
    meta = S.from_dict(S.MetaSkill, records[0])
    protocol = json.loads((root / 'l2_manifest.json').read_text())
    mode = protocol['config'].get('skill_edit_mode', 'rewrite')
    acceptance_mode = protocol['config'].get('acceptance_mode', 'predicted')
    predicted_scope = protocol['config'].get('predicted_review_scope', 'val')
    require(protocol.get('acceptance_mode', acceptance_mode) == acceptance_mode,
            'L2 manifest acceptance mode disagrees with frozen config')
    runner = SkillPatchRunner(
        SkillEditor(None, meta, skill_edit_mode=mode), None,
        root / 'l2_proposals', read_only=True,
        acceptance_mode=acceptance_mode,
        predicted_review_scope=predicted_scope,
    )
    pattern_path = root / 'l2_patterns' / (batch['batch_id'] + '.json')
    patterns = json.loads(pattern_path.read_text())
    require(batch['batch_patterns'] == patterns, 'batch pattern journal mismatch')
    validate_cache(patterns, cards)
    result = runner.run(base, cards, batch['requested_candidates'],
                        batch_patterns=patterns['patterns'],
                        acceptance_record=batch.get('acceptance')
                        if acceptance_mode in ('empirical', 'jev') or
                        (acceptance_mode == 'predicted' and predicted_scope == 'val')
                        else None)
    require(all(batch.get(k) == v for k, v in result.record.items()),
            'L2 journal differs from cached proposal/review replay')
    require(batch.get('candidate') == (S.to_dict(result.candidate) if result.candidate else None),
            'committed candidate differs from replayed decision')
    if acceptance_mode == 'predicted' and predicted_scope == 'train_cards':
        # v6 predicted review is a paired-outcome protocol.  Keep the derived
        # effect for selection, but require the raw old/new outcomes to remain
        # auditable in every card judgment.
        for review in result.record.get('reviews', ()):
            for judgment in review.get('judgments', ()):
                require(judgment.get('old_outcome') in CR_OUTCOMES and
                        judgment.get('new_outcome') in CR_OUTCOMES,
                        'predicted review lacks canonical old/new outcomes')
    if acceptance_mode == 'predicted' and predicted_scope == 'val':
        acceptance = result.record.get('acceptance', {})
        require(acceptance.get('mode') == 'predicted' and
                acceptance.get('scope') == 'val',
                'predicted val acceptance scope is missing')
        require(acceptance.get('executions', 0) == 0,
                'predicted val acceptance executed benchmark episodes')
        candidate_ids = {row.get('candidate_id') for row in acceptance.get('candidates', ())}
        proposed_ids = {
            row['edit']['candidate']['candidate_id']
            for row in batch.get('proposals', [])
            if row.get('edit', {}).get('candidate')
        }
        if not proposed_ids:
            require(candidate_ids == proposed_ids,
                    'predicted val acceptance does not cover every proposed candidate')
            require(not acceptance.get('candidates'),
                    'predicted val acceptance has candidates without proposals')
            return
        if not acceptance.get('task_ids'):
            require(not candidate_ids,
                    'empty predicted val panel contains scored candidates')
            require(set(acceptance.get('candidate_ids', ())) == proposed_ids,
                    'empty predicted val panel lost candidate coverage')
            return
        require(candidate_ids == proposed_ids,
                'predicted val acceptance does not cover every proposed candidate')
        require(isinstance(acceptance.get('panel'), str) and acceptance.get('panel'),
                'predicted val acceptance has no frozen panel')
        require(acceptance.get('task_ids'), 'predicted val panel is empty')
        for row in acceptance.get('candidates', ()):
            result_value = row.get('result', {})
            require(result_value.get('task_ids') == acceptance.get('task_ids'),
                    'predicted val candidate panel differs from acceptance panel')
            require(result_value.get('base_skill_key') == batch['base_skill_key'],
                    'predicted val base Skill differs from batch head')
            require(isinstance(result_value.get('metrics', {}).get('success_delta'), (int, float)),
                    'predicted val candidate lacks success delta')
    if mode == 'structured':
        candidates = []
        for proposal in batch.get('proposals', []):
            raw_candidate = proposal.get('edit', {}).get('candidate')
            if raw_candidate:
                candidates.append(S.from_dict(S.CandidateSkill, raw_candidate))
        for candidate in candidates:
            require(len(candidate.edits) == 1, 'structured candidate must carry one edit')
            edit = candidate.edits[0]
            require(edit.section in SS.SECTION_NAMES, 'structured candidate has unknown section')
            require(edit.op in ('ADD', 'EDIT'), 'structured candidate has unsupported edit op')
            require(isinstance(edit.text, str) and edit.text.strip(),
                    'structured candidate has empty edit text')
            operation = {
                'op': 'add' if edit.op == 'ADD' else 'replace',
                'section': edit.section,
                'target_id': edit.target_id,
                'text': edit.text,
            }
            replayed = SS.render(SS.apply_edit(SS.from_legacy(base.body), operation))
            require(replayed == candidate.skill.body,
                    'structured candidate body does not match its recorded operation')


def audit_round(root, round_index):
    root = Path(root)
    directory = root / 'evolution' / f'round-{round_index}'
    manifest = json.loads((directory / 'manifest.json').read_text())
    inputs = json.loads((directory / 'input.json').read_text())
    split = json.loads((root / 'split.json').read_text())
    progressive = bool(json.loads((root / 'config.json').read_text()).get('benchmark', {}).get('progressive_library', False))
    mapping = (json.loads((root / 'task_skill_map.json').read_text())
               if (root / 'task_skill_map.json').exists() else {})
    train = sorted(int(t) for t, value in split['assignment'].items() if value == S.SPLIT_TRAIN)
    require(manifest['task_ids'] == train == inputs['task_ids'], 'round/train coverage mismatch')
    heads = {s['family_id']: S.from_dict(S.Skill, s) for s in inputs['skills']}
    heads_by_id = {s.skill_id: s for s in heads.values()}
    protocol = json.loads((root / 'l2_manifest.json').read_text())
    expected_acceptance_mode = protocol['config'].get('acceptance_mode', 'predicted')
    expected_predicted_scope = protocol['config'].get('predicted_review_scope', 'val')
    require(protocol.get('acceptance_mode', expected_acceptance_mode) == expected_acceptance_mode,
            'L2 manifest acceptance mode disagrees with frozen config')
    require(expected_predicted_scope in ('val', 'train_cards'),
            'unknown predicted review scope in frozen config')
    require(inputs['round'] == manifest['round'] == round_index, 'round identity mismatch')
    require(len(heads) == len(inputs['skills']), 'duplicate input Skill family')
    if round_index == 1:
        expected_heads = {s['skill_id']: S.from_dict(S.Skill, s).key for s in protocol['initial']}
    else:
        previous = json.loads((root / 'evolution' / f'round-{round_index-1}' / 'summary.json').read_text())
        require(previous['status'] == 'complete', 'previous round is incomplete')
        expected_heads = previous['skills']
    require({s.skill_id: s.key for s in heads.values()} == expected_heads,
            'round input does not match previous output')
    history = [S.from_dict(S.Skill, s) for s in ST.read_jsonl(root / 'skills.jsonl', repair_tail=False)]
    library = {s.key: s for s in history}
    require(len(library) == len(history), 'duplicate stored Skill version')
    for skill in heads.values():
        require(library.get(skill.key) == skill, 'input differs from stored Skill')
    adapter = resolve(OmegaConf.load(root / 'config.json'))
    require(manifest['skill_keys'] == {f: s.key for f, s in heads.items()}, 'input Skill keys mismatch')
    expected_tasks = tuple(sorted(manifest['task_ids']))
    require({p.name for p in (directory / 'cards').glob('*.json')} ==
            {f'{t}.json' for t in train}, 'unexpected/missing card files')
    cards = {}
    for task_id in expected_tasks:
        path = directory / 'cards' / f'{task_id}.json'
        value = json.loads(path.read_text())
        exp = S.from_dict(S.TaskExperience, value)
        require(exp.task_id == task_id, f'card filename/task mismatch: {task_id}')
        require(exp.split == S.SPLIT_TRAIN, f'non-train card: {task_id}')
        require(exp.evolution_round == round_index, f'wrong card round: {task_id}')
        require(exp.benchmark == split['benchmark'], 'card benchmark mismatch')
        if progressive:
            require(exp.selected_skill_id in heads_by_id,
                    'card selected an unknown Skill')
        else:
            require(exp.selected_skill_id == mapping[str(task_id)], 'card benchmark/routing mismatch')
        expected_skill = manifest['skill_keys'].get(exp.family_id)
        require(expected_skill == exp.initial_skill_key,
                f'card {task_id} was executed with a different Skill head')
        require(S.content_hash(value) == manifest['cards'][str(task_id)],
                f'card hash mismatch: {task_id}')
        require(exp.selection_source == (S.SELECTION_AGENT if progressive else S.SELECTION_FIXED) and
                exp.selected_skill_id == heads[exp.family_id].skill_id, 'card selection mismatch')
        if progressive:
            require(exp.skill_load and exp.skill_load.get('skill_id') == exp.selected_skill_id,
                    'card Skill load provenance mismatch')
            require(exp.skill_load.get('load_stage') == 'after_selection',
                    'progressive Skill was loaded before selection')
            require(all('body' not in item and 'key' not in item for item in exp.selection_catalog),
                    'card selector catalog leaked Skill body')
        checkpoint = json.loads((directory / 'trials' / f'{task_id}.json').read_text())
        if progressive and split['benchmark'] == 'terminalbench':
            from skillexpand.benchmarks.terminalbench import audit_harbor_experience
            audit_harbor_experience(exp)
        else:
            require(checkpoint['experience'] == value, f'card/checkpoint mismatch: {task_id}')
            require(checkpoint['identity']['skill'] ==
                    {'key': expected_skill, 'body': heads[exp.family_id].body}, 'executed Skill body mismatch')
            require(checkpoint['identity']['k'] == protocol['l1']['attempts'] and
                    checkpoint['identity']['supervised'] == protocol['l1']['supervised'], 'L1 budget changed')
            audit_checkpoint(checkpoint, adapter)
        cards[task_id] = exp

    planned = json.loads((directory / 'batches.json').read_text())
    journals = []
    journal_dir = root / 'l2_batches'
    if journal_dir.exists():
        for path in sorted(journal_dir.glob('*.json')):
            value = json.loads(path.read_text())
            if value.get('round') == round_index:
                journals.append(value)
    require({b['batch_id'] for b in journals} == {b['batch_id'] for b in planned},
            'missing or unexpected L2 journals')
    require(len(journals) == len(planned) == len({b['batch_id'] for b in journals}),
            'duplicate L2 journal or plan')
    by_id = {b['batch_id']: b for b in journals}
    journals = [by_id[b['batch_id']] for b in planned]
    seen = []
    for plan, batch in zip(planned, journals):
        require(all(batch.get(k) == v for k, v in plan.items()), 'batch differs from frozen plan')
        base = heads[batch['family_id']]
        require(batch['base_skill_key'] == base.key, 'broken sequential Skill chain')
        require(bool(batch.get('candidate')) == (batch['outcome'] == 'review_approved'),
                'approval/candidate mismatch')
        require(batch['outcome'] != 'review_approved' or
                bool(batch['reviews']) or
                (expected_acceptance_mode == 'predicted' and expected_predicted_scope == 'val'),
                'approval without reviews')
        audit_batch(root, batch, base, [cards[t] for t in batch['task_ids']])
        require(batch.get('card_hashes'), 'round batch has no card hashes')
        for task_id in batch['task_ids']:
            require(task_id in cards, f'batch references task outside round: {task_id}')
            require(batch['card_hashes'].get(str(task_id)) ==
                    S.content_hash(S.to_dict(cards[task_id])),
                    f'batch/card hash mismatch: {task_id}')
            seen.append(task_id)
            require(cards[task_id].family_id == batch['family_id'], 'cross-family batch')
        if batch.get('candidate'):
            require(batch['outcome'] == 'review_approved',
                    'candidate is present on a non-approved journal')
            require(batch['candidate']['candidate_id'] == batch['selected_candidate_id'],
                    'selected candidate does not match journal candidate')
            candidate = S.from_dict(S.CandidateSkill, batch['candidate'])
            require(candidate.base_skill_key == base.key and candidate.skill.version == base.version + 1
                    and candidate.skill.description == base.description, 'invalid candidate version/description')
            require(library.get(candidate.skill.key) == candidate.skill, 'journal/Skill store mismatch')
            heads[batch['family_id']] = candidate.skill
        expected_mode = expected_acceptance_mode
        require(batch.get('acceptance_mode') == expected_mode,
                'batch acceptance mode differs from frozen protocol')
        require(batch.get('acceptance', {}).get('mode') == expected_mode,
                'batch acceptance record mode differs from frozen protocol')
        if expected_mode == 'predicted':
            require(batch.get('empirically_validated') is False,
                    'prediction was mislabeled as empirical validation')
            require(batch.get('acceptance', {}).get('executions', 0) == 0,
                    'predicted acceptance executed val tasks')
            require(batch.get('predicted_review_scope') == expected_predicted_scope,
                    'batch predicted review scope mismatch')
            require(batch.get('acceptance', {}).get('scope') == expected_predicted_scope,
                    'predicted acceptance scope mismatch')
        elif expected_mode == 'empirical':
            require(batch.get('empirically_validated') is bool(batch.get('acceptance', {}).get('candidates')),
                    'empirical validation flag mismatch')
            acceptance = batch.get('acceptance', {})
            if acceptance.get('candidates'):
                candidate_ids = {row.get('candidate_id') for row in acceptance['candidates']}
                proposed_ids = {
                    row['edit']['candidate']['candidate_id']
                    for row in batch.get('proposals', [])
                    if row.get('edit', {}).get('candidate')
                }
                require(candidate_ids == proposed_ids,
                        'empirical acceptance does not cover every proposed candidate')
        else:
            require(batch.get('empirically_validated') is False,
                    'JEV acceptance was mislabeled as empirical validation')
            require(batch.get('jev_validated') is bool(batch.get('acceptance', {}).get('candidates')),
                    'JEV validation flag mismatch')
            acceptance = batch.get('acceptance', {})
            require(acceptance.get('executions', 0) == 0,
                    'JEV acceptance executed benchmark episodes')
            if acceptance.get('candidates'):
                candidate_ids = {row.get('candidate_id') for row in acceptance['candidates']}
                proposed_ids = {
                    row['edit']['candidate']['candidate_id']
                    for row in batch.get('proposals', [])
                    if row.get('edit', {}).get('candidate')
                }
                require(candidate_ids == proposed_ids,
                        'JEV acceptance does not cover every proposed candidate')
    require(tuple(sorted(seen)) == expected_tasks,
            'round batches do not cover each train task exactly once')
    for skill in heads.values():
        require(library.get(skill.key) == skill, 'round output differs from Skill history')
    summary_path = directory / 'summary.json'
    if summary_path.exists():
        summary = json.loads(summary_path.read_text())
        require(summary.get('status') == 'complete', 'round summary is incomplete')
        require(summary.get('train_cards') == len(expected_tasks),
                'round summary card count mismatch')
        expected_mode = expected_acceptance_mode
        require(summary.get('acceptance_mode') == expected_mode,
                'summary acceptance mode mismatch')
        require(summary.get('predicted_review_scope', 'val') == expected_predicted_scope,
                'summary predicted review scope mismatch')
        if expected_mode == 'predicted':
            require(summary.get('val_executions') == 0,
                    'val execution leaked into predicted evolution')
        elif expected_mode == 'jev':
            expected_jev_validated = bool(journals) and all(
                bool(b.get('acceptance', {}).get('candidates')) for b in journals
            )
            require(summary.get('empirically_validated') is False and
                    summary.get('jev_validated') is expected_jev_validated,
                    'JEV summary validation flags mismatch')
            require(summary.get('val_executions') == 0,
                    'JEV summary counted judge requests as val executions')
            require(summary.get('jev_requests') == sum(
                int(b.get('acceptance', {}).get('jev_requests', 0)) for b in journals
            ), 'JEV request count mismatch')
        require(summary['skills'] == {s.skill_id: s.key for s in heads.values()}, 'summary Skill mismatch')
        require(summary['completed_batches'] == summary['batches'] == len(journals), 'batch count mismatch')
        require(summary['review_approved_updates'] == sum(b['outcome'] == 'review_approved' for b in journals),
                'approval count mismatch')
        if expected_mode == 'predicted' and expected_predicted_scope == 'val':
            require(summary.get('predicted_val_candidates') == sum(
                len(b.get('acceptance', {}).get('candidates', ())) for b in journals
            ), 'predicted val candidate count mismatch')
    return {'round': round_index, 'tasks': len(cards), 'batches': len(journals),
            'review_approved': sum(x.get('outcome') == 'review_approved' for x in journals),
            'acceptance_mode': expected_acceptance_mode,
            'empirical_validation': expected_acceptance_mode == 'empirical'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--round', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = dict(integrity='passed', **audit_round(args.run_dir, args.round))
        save(args.output, result)
        print(json.dumps(result))
    except (OSError, ValueError, KeyError, TypeError, ST.StoreError) as exc:
        result = {'integrity': 'failed', 'error': str(exc)}
        save(args.output, result)
        print(json.dumps(result))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
