"""Offline integrity checks for one Skill-aware L1 -> L2 evolution round."""
import argparse
import json
from pathlib import Path

from skillexpand import schema as S
from skillexpand.persistence.io import require, save
from skillexpand.persistence import io as IO
from skillexpand.persistence import store as ST
from skillexpand.reliability.errors import StoreError
from skillexpand.l2.editor import SkillEditor
from skillexpand.l2.update import SkillPatchRunner
from skillexpand.l2.card_review import OUTCOMES as CR_OUTCOMES
from skillexpand.l1.patterns import validate_cache
from skillexpand.l1.audit import audit_checkpoint
from skillexpand.l1.adapters import resolve
from omegaconf import OmegaConf
from skillexpand import structured_skill as SS
from skillexpand.l2 import reviewer_coevolution as RC




def audit_memories(root, batch):
    """Both memories must be exactly the views the ledger yields at this round.

    The Planner's memory is checkable in full: it is recomputed here and compared
    character for character, so any task text that found its way into it would
    make the two differ.  The Reviewer's is checkable by coverage: it may draw on
    every proposal from an earlier round and on nothing else.
    """
    from skillexpand.l2 import memory as MEM

    round_index = int(batch.get('round', 0))
    if round_index < 1:
        return
    changes = MEM.read_changes(root, before_round=round_index)
    expected = MEM.PlannerMemory(changes).render()
    require(batch.get('planner_memory', '') == expected,
            'journaled Planner memory is not the aggregate the ledger yields')
    require(batch.get('reviewer_memory_candidates', -1)
            == len({change.candidate_id for change in changes}),
            'journaled Reviewer memory does not cover exactly the earlier proposals')

def audit_sampled_batch(batch):
    """Replay a sampled-acceptance batch from its journal alone.

    Three things must hold, and each of them is a way the protocol could quietly
    stop being the protocol: the claim must still bind the proposal, the executed
    subset must be the one the frozen key selects out of the frozen panel, and
    the recorded decision must be recomputable from the recorded per-task rows.
    """
    from skillexpand.evaluation import ppi as PPI

    hypotheses = batch.get('hypotheses', ())
    require(hypotheses, 'sampled batch recorded no hypotheses')
    for row in hypotheses:
        # The claim binds the proposal to what the verifier will check, and its
        # id is assigned by the program.  A journal that lost or rewrote the
        # claim cannot be replayed, so the mismatch is fatal here.
        claim = row.get('claim')
        require(isinstance(claim, dict), 'sampled hypothesis has no claim')
        require(set(claim) == {'trigger', 'action_change', 'claim_id'},
                'sampled claim has an unexpected shape')
        try:
            rebuilt = S.Claim(claim['trigger'], claim['action_change'])
        except (ValueError, TypeError) as exc:
            require(False, f'sampled claim is invalid: {exc}')
        require(claim['claim_id'] == rebuilt.claim_id,
                'claim id does not match the claim text it accompanies')
    materialized = [row for row in batch.get('proposals', ())
                    if row.get('edit', {}).get('candidate')]
    require(all(row.get('claim') is not None for row in materialized),
            'sampled proposal materialized a candidate without a claim')

    acceptance = batch.get('acceptance') or {}
    if not materialized:
        require(not acceptance.get('candidates'),
                'sampled acceptance scored candidates that were never proposed')
        return
    require(acceptance.get('mode') == 'sampled', 'sampled acceptance mode is missing')
    require(acceptance.get('scope') == 'val', 'sampled acceptance scope is missing')
    panel = tuple(int(t) for t in acceptance.get('task_ids', ()))
    require(panel, 'sampled acceptance recorded an empty val panel')
    require(tuple(sorted(panel)) == panel, 'sampled acceptance panel is not in fixed order')
    confidence = acceptance.get('confidence')
    require(isinstance(confidence, (int, float)) and 0.0 < float(confidence) < 1.0,
            'sampled acceptance recorded an invalid confidence level')
    sample_size = int(acceptance.get('sample_size', 0))
    require(sample_size >= 2, 'sampled acceptance recorded an unusable sample size')
    require(int(acceptance.get('executions', 0)) > 0,
            'sampled acceptance recorded no executed episode')
    require(int(acceptance.get('predicted_requests', 0)) > 0,
            'sampled acceptance recorded no reviewer request')

    proposed = {row['edit']['candidate']['candidate_id'] for row in materialized}
    results = list(acceptance.get('candidates', ()))
    require({row.get('candidate_id') for row in results} == proposed,
            'sampled acceptance does not cover every proposed candidate')
    for row in results:
        result = row.get('result') or {}
        require(tuple(int(t) for t in result.get('panel_task_ids', ())) == panel,
                'sampled candidate panel differs from the acceptance panel')
        require(result.get('base_skill_key') == batch['base_skill_key'],
                'sampled base Skill differs from the batch head')
        require(result.get('claim_id'),
                'sampled candidate is not bound to a claim')
        sample = tuple(int(t) for t in result.get('sample_task_ids', ()))
        require(sample, 'sampled candidate recorded no executed sample')
        require(len(set(sample)) == len(sample), 'sampled candidate repeated a task')
        require(set(sample) <= set(panel),
                'sampled candidate executed a task outside the frozen panel')
        expected = PPI.select_sample(panel, sample_size, result.get('sample_key', ''))
        require(sample == expected,
                'executed sample is not the one the recorded key selects')
        # The requested size is a ceiling: a family with a smaller panel is
        # measured whole, and the recorded count must show that rather than the
        # requested number.
        require(int((result.get('decision') or {}).get('n_sample', -1))
                == PPI.effective_sample_size(len(panel), sample_size),
                'executed count is not the per-panel effective sample size')

        rows = list(result.get('rows', ()))
        require([int(r['task_id']) for r in rows] == list(panel),
                'sampled per-task rows do not cover the panel in fixed order')
        measured = {int(r['task_id']): float(r['measured_delta'])
                    for r in rows if r.get('sampled')}
        require(set(measured) == set(sample),
                'sampled rows disagree with the executed sample')
        predictions = {int(r['task_id']): float(r['delta_probability'])
                       for r in rows}
        decision = PPI.estimate(predictions, measured, confidence=float(confidence))
        recorded = result.get('decision') or {}
        require(bool(recorded.get('accepted')) == decision.accepted,
                'recorded sampled decision is not the one the rows imply')
        for key, value in (('point', decision.point), ('lower', decision.lower)):
            require(abs(float(recorded.get(key, 0.0)) - float(value)) < 1e-9,
                    f'recorded sampled {key} does not match the recomputed estimate')

        # Attribution is a diagnostic, and two invariants keep it one: it must
        # only ever describe a difference that was actually observed, and it must
        # cover every observed difference when it is enabled -- a partial panel
        # would let the weakest cases drop out.
        from skillexpand.evaluation.claim_check import CATEGORIES

        enabled = result.get('verification_enabled')
        require(isinstance(enabled, bool),
                'sampled result does not record whether verification ran')
        for task_row in rows:
            divergence = task_row.get('divergence')
            verification = task_row.get('verification')
            if not task_row.get('sampled'):
                require(divergence is None and verification is None,
                        'unexecuted task recorded a trajectory difference')
                continue
            require(verification is None or divergence is not None,
                    'verification recorded without an execution difference')
            if not enabled:
                require(verification is None,
                        'verification recorded although it was disabled')
                continue
            require((divergence is None) == (verification is None),
                    'an observed execution difference was left unverified')
            if verification is not None:
                require(verification.get('category') in CATEGORIES,
                        'verifier returned an unknown category')
                require(isinstance(verification.get('reason'), str)
                        and verification['reason'].strip(),
                        'verifier returned an empty reason')

def audit_batch(root, batch, base, cards):
    """Replay cached decisions without any model, environment, or file writes."""
    root = Path(root)
    protocol = json.loads((root / 'l2_manifest.json').read_text())
    config = protocol['config']
    mode = config['skill_edit_mode']
    acceptance_mode = config['acceptance_mode']
    predicted_scope = config['predicted_review_scope']
    runner = SkillPatchRunner(
        SkillEditor(None, skill_edit_mode=mode), None,
        root / 'l2_proposals', read_only=True,
        acceptance_mode=acceptance_mode,
        predicted_review_scope=predicted_scope,
        single_candidate=config['single_candidate'],
    )
    pattern_path = root / 'l2_patterns' / (batch['batch_id'] + '.json')
    patterns = json.loads(pattern_path.read_text())
    require(batch['batch_patterns'] == patterns, 'batch pattern journal mismatch')
    validate_cache(patterns, cards)
    result = runner.run(base, cards, batch['requested_candidates'],
                        batch_patterns=patterns['patterns'],
                        acceptance_record=batch.get('acceptance')
                        if acceptance_mode in ('empirical', 'jev', 'sampled') or
                        (acceptance_mode == 'predicted' and predicted_scope == 'val')
                        else None)
    require(all(batch.get(k) == v for k, v in result.record.items()),
            'L2 journal differs from cached proposal/review replay')
    require(batch.get('candidate') == (S.to_dict(result.candidate) if result.candidate else None),
            'committed candidate differs from replayed decision')
    if acceptance_mode == 'predicted' and predicted_scope == 'train_cards':
        # Card review is a paired-outcome protocol: the effect is derived, so the
        # raw old/new outcomes must stay auditable in every card judgment.
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
    if acceptance_mode == 'sampled':
        audit_sampled_batch(batch)
        audit_memories(root, batch)
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
    if protocol['config']['single_candidate']:
        require(batch.get('single_candidate') is True,
                'single-candidate protocol missing from batch journal')
        require(batch.get('requested_candidates') == 1,
                'single-candidate batch requested a different K')
        proposed = [row for row in batch.get('proposals', ())
                    if row.get('edit', {}).get('candidate')]
        require(len(proposed) <= 1,
                'single-candidate batch contains multiple materialized candidates')
        require(len(batch.get('acceptance', {}).get('candidates', ())) <= 1,
                'single-candidate acceptance scored multiple candidates')


def audit_round(root, round_index):
    root = Path(root)
    directory = root / 'evolution' / f'round-{round_index}'
    manifest = json.loads((directory / 'manifest.json').read_text())
    inputs = json.loads((directory / 'input.json').read_text())
    split = json.loads((root / 'split.json').read_text())
    mapping = json.loads((root / 'task_skill_map.json').read_text())
    train = sorted(int(t) for t, value in split['assignment'].items() if value == S.SPLIT_TRAIN)
    require(manifest['task_ids'] == train == inputs['task_ids'], 'round/train coverage mismatch')
    heads = {s['family_id']: S.from_dict(S.Skill, s) for s in inputs['skills']}
    protocol = json.loads((root / 'l2_manifest.json').read_text())
    expected_acceptance_mode = protocol['config']['acceptance_mode']
    expected_predicted_scope = protocol['config']['predicted_review_scope']
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
        require(exp.benchmark == split['benchmark'] and exp.selected_skill_id == mapping[str(task_id)],
                'card benchmark/routing mismatch')
        expected_skill = manifest['skill_keys'].get(exp.family_id)
        require(expected_skill == exp.initial_skill_key,
                f'card {task_id} was executed with a different Skill head')
        require(S.content_hash(value) == manifest['cards'][str(task_id)],
                f'card hash mismatch: {task_id}')
        require(exp.selection_source == S.SELECTION_FIXED and
                exp.selected_skill_id == heads[exp.family_id].skill_id, 'card selection mismatch')
        checkpoint = json.loads((directory / 'trials' / f'{task_id}.json').read_text())
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
        require(summary.get('predicted_review_scope') == expected_predicted_scope,
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
    if protocol['config'].get('reviewer_update_mode', 'none') != 'none':
        # Later rounds append to shared ledgers. A historical round audit must
        # replay the evidence available at its boundary, even during resume.
        all_feedback = IO.read_jsonl(root / 'reviewer_feedback.jsonl', repair_tail=False)
        all_updates = IO.read_jsonl(root / 'reviewer_updates.jsonl', repair_tail=False)
        known_rounds = {int(json.loads(path.read_text())['round'])
                        for path in (root / 'evolution').glob('round-*/input.json')}
        RC.validate_feedback_records(all_feedback, strict=True)
        require(all(int(row.get('round_index', 0)) in known_rounds for row in all_feedback),
                'feedback references an unstarted round')
        require(all(int(row.get('generation_round', 0)) in known_rounds for row in all_updates),
                'Reviewer update references an unstarted round')
        feedback_rows = tuple(row for row in all_feedback
                              if int(row.get('round_index', 0)) <= round_index)
        updates = tuple(row for row in all_updates
                        if int(row.get('generation_round', 0)) <= round_index)
        if feedback_rows:
            require(updates, 'Reviewer feedback has no versioned update artifact')
            train_ids = {int(task_id) for task_id, value in split['assignment'].items()
                         if value == S.SPLIT_TRAIN}
            feedback_ids = set()
            groups = {}
            for row in feedback_rows:
                require(int(row.get('round_index', 0)) <= round_index,
                        'feedback row is ahead of audited round')
                task_id = int(row.get('task_id', -1))
                require(task_id in train_ids, 'feedback task is not in frozen train split')
                base_skill_id = str(row['base_skill_key']).split('@', 1)[0]
                candidate_skill_id = str(row['candidate_skill_key']).split('@', 1)[0]
                require(base_skill_id == candidate_skill_id,
                        'feedback arms refer to different Skill identities')
                require(mapping.get(str(task_id)) == base_skill_id,
                        'feedback task does not belong to its Skill family')
                family = base_skill_id.rsplit('.', 1)[-1]
                require(row.get('family_id') == family,
                        'feedback family does not match task mapping')
                require(row.get('base_probability') is not None and
                        row.get('candidate_probability') is not None and
                        isinstance(row.get('predicted_improve'), bool),
                        'feedback is missing a complete Reviewer prediction')
                feedback_id = row.get('feedback_id')
                require(feedback_id not in feedback_ids, 'duplicate feedback ID')
                feedback_ids.add(feedback_id)
                key = (int(row['round_index']), row['batch_id'],
                       row['base_skill_key'], row['candidate_skill_key'])
                group = groups.setdefault(key, [])
                group.append(row)
            for key, group in groups.items():
                task_ids = sorted(int(row['task_id']) for row in group)
                skill_id = key[2].split('@', 1)[0]
                expected = sorted(task for task in train_ids if mapping[str(task)] == skill_id)
                feedback_size = int(protocol['config'].get('reviewer_feedback_size', 0))
                if feedback_size:
                    expected = expected[:feedback_size]
                require(task_ids == expected,
                        'feedback panel does not cover the frozen family subset exactly')
                provenance = {(row.get('route_fingerprint'), row.get('panel_key'),
                               row.get('executor_protocol'),
                               row.get('reviewer_protocol_hash'),
                               int(row.get('reviewer_prompt_version', -1))) for row in group}
                require(len(provenance) == 1, 'feedback provenance differs within a pair')
                expected_version = max(
                    (int(update['reviewer_prompt_version']) for update in updates
                     if int(update.get('generation_round', 0)) < key[0]),
                    default=0,
                )
                require(next(iter(provenance))[4] == expected_version,
                        'feedback used an unexpected Reviewer prompt version')
            expected_pairs = set()
            for path in (root / 'l2_batches').glob('*.json'):
                batch = json.loads(path.read_text())
                batch_round = int(batch.get('round', 0))
                if batch_round < 1 or batch_round > round_index:
                    continue
                found_candidate = False
                for proposal in batch.get('proposals', ()):
                    raw = (proposal.get('edit', {}).get('candidate') or
                           proposal.get('candidate'))
                    if raw:
                        found_candidate = True
                        candidate = S.from_dict(S.CandidateSkill, raw)
                        expected_pairs.add((batch_round, batch['batch_id'],
                                            batch['base_skill_key'],
                                            candidate.skill.key))
                if not found_candidate and batch.get('candidate'):
                    candidate = S.from_dict(S.CandidateSkill, batch['candidate'])
                    expected_pairs.add((batch_round, batch['batch_id'],
                                        batch['base_skill_key'], candidate.skill.key))
            require(set(groups) == expected_pairs,
                    f'feedback does not cover every materialized candidate exactly once: '
                    f'actual={sorted(groups)!r} expected={sorted(expected_pairs)!r}')
            versions = []
            for index, update in enumerate(updates):
                require(update.get('protocol') == RC.PROTOCOL,
                        'Reviewer update protocol mismatch')
                ids = set(update.get('feedback_ids', ()))
                require(ids and ids <= feedback_ids,
                        'Reviewer update references unknown feedback')
                generation = int(update.get('generation_round', 0))
                require(0 < generation <= round_index,
                        'Reviewer update round is ahead of audited round')
                expected_ids = {row['feedback_id'] for row in feedback_rows
                                if int(row.get('round_index', 0)) == generation}
                require(ids == expected_ids,
                        'Reviewer update does not cover exactly its generation feedback')
                version = int(update.get('reviewer_prompt_version', 0))
                parent = update.get('parent_version')
                if protocol['config'].get('reviewer_update_mode') == 'rules':
                    generation_rows = tuple(S.from_dict(RC.PairedFeedback, row)
                                            for row in feedback_rows
                                            if int(row['round_index']) == generation)
                    if update.get('skip_reason') == 'no_observed_rules':
                        computed = RC.summarize_feedback(generation_rows)
                        require(not RC._rules_from_feedback(generation_rows, computed),
                                'empty-rules update skipped available rule evidence')
                        require(update.get('generator') == 'program' and
                                update.get('rules') == [] and not update.get('raw_output') and
                                update.get('summary') == computed and
                                update.get('calibration_block') == RC.render_calibration_block(computed, ()) and
                                update.get('input_prompt') == RC.update_prompt(computed, ()),
                                'empty-rules update differs from computed feedback')
                    else:
                        require(not update.get('skip_reason') and
                                update.get('generator') == 'llm' and
                                update.get('input_prompt') and update.get('raw_output'),
                                'rules update lacks Reviewer generation provenance')
                else:
                    require(update.get('generator', 'program') == 'program',
                            'summary update was generated by an unexpected mechanism')
                require(version == (versions[-1] + 1 if versions else 1),
                        'Reviewer update versions are not contiguous')
                require(parent == (versions[-1] if versions else None),
                        'Reviewer update parent chain is broken')
                versions.append(version)
        else:
            require(not updates,
                    'Reviewer update exists without any paired feedback records')
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
    except (OSError, ValueError, KeyError, TypeError, StoreError) as exc:
        result = {'integrity': 'failed', 'error': str(exc)}
        save(args.output, result)
        print(json.dumps(result))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
