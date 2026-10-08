"""Offline checks of the sampled-acceptance protocol.

The protocol is defined over three contracts that must not drift apart: the
Planner's claim, the Reviewer's paired delta, and the sampling decision.  These
tests pin each one without a model or an environment.
"""

import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from skillexpand import schema as S
from skillexpand import structured_skill as SS
from skillexpand.l2 import sampled as SM
from skillexpand.l2 import update as UP
from skillexpand.l2.editor import SkillEditor
from skillexpand.evaluation import ppi as PPI
from skillexpand.evaluation import validation as V
from skillexpand.evaluation.delta_review import PairedDeltaReviewer
from skillexpand.evaluation.claim_check import CATEGORIES, TrajectoryVerifier, trajectory_view
from skillexpand.evaluation.sampled_validation import SampledDeltaValidator
from skillexpand.runtime import agent_factory as F
from skillexpand.persistence.io import AuditFailure
from skillexpand import campaign as C
from skillexpand.reliability.errors import FrozenProtocolChanged, InvalidInput
from skillexpand.l2 import ledger as LED
from skillexpand.l2 import memory as MEM
from skillexpand.l2 import sampled_audit as SA

EVIDENCE_ID = 't1:e1'

#: The frozen protocol switches the offline audits replay against.
CONFIG = dict(SM.DEFAULTS, acceptance_sample_size=4)


def audit(root, record, round_index=1):
    """Audit a runner record as the loop journals it (round and memories added)."""
    SA.audit_batch(root, dict(record, round=round_index, planner_memory='',
                              reviewer_memory_version=0), CONFIG)


def fake_experience(task_id=1, body_claims=()):
    """The minimum a batch card needs to be projected into a Planner payload."""
    card = {
        'schema_version': 5,
        'card_id': f'discovery:0:{task_id}',
        'task': {'task_id': task_id, 'benchmark': 'searchqa', 'family_id': 'f',
                 'text': 'Question: who wrote it?'},
        'execution': {'round': 0, 'skill_key': None, 'trials': [], 'success': True},
        'evidence': [{'id': EVIDENCE_ID, 'trial': 1, 'phase': 'autonomous',
                      'action': 'Search[author]', 'observation': 'observed',
                      'observation_truncated': False, 'effect': 'observed',
                      'method': True}],
        'claims': list(body_claims),
        'claim_status': 'empty_by_model',
    }
    return SimpleNamespace(experience_id=card['card_id'], experience_card=card,
                           task_id=task_id, split=S.SPLIT_TRAIN, family_id='f',
                           selected_skill_id='searchqa.f', initial_skill_key=None)


def hypothesis(**overrides):
    row = {
        'mechanism': 'the rule never fires on a closed receptacle',
        'change': 'require opening the receptacle before placing the object',
        'evidence': [{'card_id': 'discovery:0:1', 'evidence_id': EVIDENCE_ID}],
        'edit': {'op': 'add', 'section': 'conditions', 'target_id': None,
                 'text': 'If the receptacle is closed, open it before placing the object.'},
    }
    row.update(overrides)
    return row


class _PlannerHost:
    """Scripted Planner: one hypothesis with a claim and a structured edit."""

    def __init__(self, claim):
        self.claim = claim
        self.prompts = []

    def llm(self, messages, **kwargs):
        self.prompts.append('\n'.join(str(getattr(m, 'content', m)) for m in messages))
        return json.dumps({'hypotheses': [hypothesis(
            claim={'trigger': self.claim.trigger,
                   'action_change': self.claim.action_change})]})


class RecordingHost:
    """Captures the prompt so the contract text can be asserted on."""

    def __init__(self):
        self.messages = ()

    def llm(self, messages, **kwargs):
        self.messages = tuple(messages)
        return '{"hypotheses":[]}'

    @property
    def prompt(self):
        return '\n'.join(str(getattr(m, 'content', m)) for m in self.messages)


class ClaimTests(unittest.TestCase):
    def test_claim_normalizes_whitespace_and_derives_its_own_id(self):
        claim = S.Claim('  the target receptacle is closed  ',
                        '  open it before placing the object  ')
        self.assertEqual(claim.trigger, 'the target receptacle is closed')
        self.assertEqual(claim.action_change, 'open it before placing the object')
        self.assertEqual(S.from_dict(S.Claim, claim.to_dict()), claim)
        self.assertEqual(claim.to_dict()['claim_id'], claim.claim_id)

    def test_claim_rejects_empty_multiline_repeated_and_oversized_text(self):
        bad = [
            ('', 'action'),
            ('trigger', '   '),
            ('a\nb', 'action'),
            ('trigger', 'action\rchange'),
            ('same text', 'same text'),
            ('x' * (S.Claim.MAX_CHARS + 1), 'action'),
        ]
        for trigger, action in bad:
            with self.subTest(trigger=trigger[:12]), self.assertRaises(ValueError):
                S.Claim(trigger, action)

    def test_claim_is_required_exactly_under_the_sampled_protocol(self):
        experience = fake_experience()
        raw = json.dumps({'hypotheses': [hypothesis()]})
        parsed = UP.parse_plan(raw, [experience], 1, structured=True)
        self.assertNotIn('claim', parsed[0])
        with self.assertRaises(ValueError):
            UP.parse_plan(raw, [experience], 1, structured=True, require_claim=True)
        self.assertTrue(SM.claim_required('sampled'))
        self.assertFalse(SM.claim_required('predicted'))

    def test_program_assigns_the_claim_id_and_rejects_a_supplied_one(self):
        experience = fake_experience()
        row = hypothesis(claim={'trigger': 't', 'action_change': 'a'})
        parsed = UP.parse_plan(json.dumps({'hypotheses': [row]}), [experience], 1,
                               structured=True, require_claim=True)
        self.assertEqual(parsed[0]['claim']['claim_id'], S.Claim('t', 'a').claim_id)
        forged = hypothesis(claim={'trigger': 't', 'action_change': 'a',
                                   'claim_id': 'forged'})
        with self.assertRaises(ValueError):
            UP.parse_plan(json.dumps({'hypotheses': [forged]}), [experience], 1,
                          structured=True, require_claim=True)

    def test_planner_prompt_carries_the_claim_contract_only_when_required(self):
        host = RecordingHost()
        editor = SkillEditor(host, skill_edit_mode='structured')
        base = S.Skill('searchqa.f', 'f', 0, 'lookup', 'scope',
                       SS.render(SS.from_sections({
                           'procedure': ['Search the named item.'],
                           'conditions': [],
                           'completion_checks': [],
                       })))
        experience = fake_experience()

        editor.plan(base, [experience], 1, batch_patterns=())
        self.assertNotIn('action_change', host.prompt)

        editor.plan(base, [experience], 1, batch_patterns=(), claim_required=True)
        # The claim sits inside the structured hard schema, not in a second one.
        self.assertIn('"change":"...",' + SM.CLAIM_FIELD, host.prompt)

    def test_sampled_requires_structured_editing_and_no_train_calibration(self):
        def options(**overrides):
            return {**SM.DEFAULTS, 'acceptance_mode': 'sampled',
                    'skill_edit_mode': 'structured', 'reviewer_update_mode': 'none',
                    **overrides}

        SM.validate_options(options())
        SM.validate_options(options(acceptance_mode='predicted', skill_edit_mode='rewrite',
                                    reviewer_update_mode='rules'))
        for bad in ({'skill_edit_mode': 'rewrite'}, {'reviewer_update_mode': 'rules'},
                    {'acceptance_sample_size': 1}, {'claim_verification': 'maybe'}):
            with self.subTest(**bad), self.assertRaises(InvalidInput):
                SM.validate_options(options(**bad))


class SamplingEstimateTests(unittest.TestCase):
    def test_sample_is_reproducible_from_the_protocol_key(self):
        panel = list(range(40))
        first = PPI.select_sample(panel, 8, 'batch-1:candidate-a')
        self.assertEqual(first, PPI.select_sample(panel, 8, 'batch-1:candidate-a'))
        self.assertEqual(len(first), 8)
        self.assertEqual(list(first), sorted(first))
        self.assertTrue(set(first) <= set(panel))
        self.assertNotEqual(first, PPI.select_sample(panel, 8, 'batch-1:candidate-b'))
        self.assertEqual(PPI.select_sample(panel, 8, 'batch-1:candidate-a'),
                         PPI.select_sample(list(reversed(panel)), 8, 'batch-1:candidate-a'))

    def test_sample_larger_than_the_panel_is_the_whole_panel(self):
        panel = [3, 1, 2]
        self.assertEqual(PPI.select_sample(panel, 10, 'k'), (1, 2, 3))
        with self.assertRaises(ValueError):
            PPI.select_sample([], 4, 'k')
        with self.assertRaises(ValueError):
            PPI.select_sample(panel, 0, 'k')

    def test_an_accurate_reviewer_needs_no_correction(self):
        panel = {t: (0.5 if t % 2 else -0.5) for t in range(10)}
        sample = {t: panel[t] for t in (1, 3, 5)}
        decision = PPI.estimate(panel, sample, confidence=0.9)
        self.assertEqual(decision.sample_mean_error, 0.0)
        self.assertAlmostEqual(decision.point, 0.0)
        self.assertAlmostEqual(decision.standard_error, 0.0)
        self.assertFalse(decision.accepted)

    def test_a_biased_reviewer_cannot_manufacture_acceptance(self):
        panel = {t: 0.6 for t in range(10)}
        sample = {t: 0.0 for t in range(10)}
        decision = PPI.estimate(panel, sample, confidence=0.9)
        self.assertAlmostEqual(decision.panel_mean_prediction, 0.6)
        self.assertAlmostEqual(decision.sample_mean_error, -0.6)
        self.assertAlmostEqual(decision.point, 0.0)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, 'lower_bound_below_zero')

    def test_a_biased_reviewer_still_accepts_a_real_improvement(self):
        # Predictions are optimistic by 0.1 everywhere, but half the sampled
        # tasks really improved.  The correction lands on the measured truth and
        # the interval still excludes zero.
        panel = {t: 0.6 for t in range(10)}
        sample = {t: (1.0 if t % 2 else 0.0) for t in range(10)}
        decision = PPI.estimate(panel, sample, confidence=0.9)
        self.assertAlmostEqual(decision.point, 0.5)
        self.assertGreater(decision.standard_error, 0.0)
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.reason, 'accepted')

    def test_identical_sampled_errors_give_a_zero_width_interval(self):
        # Documented limitation: a sample whose errors agree exactly carries no
        # measured spread, so the interval collapses.  The configured sample
        # size, not the estimator, is what keeps that from reading as certainty.
        panel = {t: 0.0 for t in range(8)}
        sample = {t: 1.0 for t in range(8)}
        decision = PPI.estimate(panel, sample, confidence=0.9)
        self.assertEqual(decision.standard_error, 0.0)
        self.assertAlmostEqual(decision.lower, 1.0)

    def test_stratified_means_keep_the_estimate_honest(self):
        # Half the panel never triggers: the prediction says +1 there, the
        # measurement says 0.  The corrected estimate must land on the truth.
        panel = {t: (1.0 if t < 20 else 0.0) for t in range(40)}
        sample = {t: 0.0 for t in range(0, 40, 5)}
        decision = PPI.estimate(panel, sample, confidence=0.9)
        self.assertAlmostEqual(decision.point, 0.0)

    def test_a_full_panel_sample_reduces_to_the_measured_mean(self):
        panel = {t: 0.4 for t in range(6)}
        sample = {t: (1.0 if t < 3 else -1.0) for t in range(6)}
        decision = PPI.estimate(panel, sample, confidence=0.9)
        self.assertAlmostEqual(decision.point, 0.0)

    def test_the_sample_size_is_capped_by_the_panel_it_draws_from(self):
        self.assertEqual(PPI.effective_sample_size(160, 16), 16)
        self.assertEqual(PPI.effective_sample_size(3, 16), 3)
        self.assertEqual(PPI.effective_sample_size(1, 16), 1)
        for bad in ((0, 4), (3, 0)):
            with self.assertRaises(ValueError):
                PPI.effective_sample_size(*bad)

    def test_a_panel_smaller_than_the_ceiling_is_measured_whole(self):
        # ALFWorld families carry a handful of val tasks: the protocol degrades
        # to a plain measurement there instead of sampling a subset.
        panel = {task_id: 0.4 for task_id in range(3)}
        sample = {task_id: float(task_id == 0) for task_id in range(3)}
        self.assertEqual(PPI.select_sample(tuple(panel), 16, 'k'), (0, 1, 2))
        decision = PPI.estimate(panel, sample, confidence=0.9)
        self.assertEqual(decision.n_sample, 3)

    def test_a_panel_below_the_minimum_cannot_decide(self):
        # One val task is the degenerate case: the estimate reduces to that
        # single measurement, carries no spread, and must not be accepted.
        decision = PPI.estimate({0: 0.9}, {0: 1.0}, confidence=0.9)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, 'insufficient_sample')
        self.assertIsNone(decision.lower)

    def test_an_unbounded_sample_yields_no_decision(self):
        panel = {t: 0.3 for t in range(4)}
        decision = PPI.estimate(panel, {0: 1.0}, confidence=0.9)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, 'insufficient_sample')
        self.assertIsNone(decision.point)
        self.assertFalse(PPI.estimate({}, {}, confidence=0.9).accepted)

    def test_estimator_rejects_malformed_input(self):
        with self.assertRaises(ValueError):
            PPI.estimate({1: 0.0}, {1: 0.0, 2: 1.0}, confidence=0.9)
        with self.assertRaises(ValueError):
            PPI.estimate({1: 0.0, 2: 0.0}, {1: 2.0, 2: 0.0}, confidence=0.9)
        with self.assertRaises(ValueError):
            PPI.estimate({1: 0.0, 2: 0.0}, {1: 0.0, 2: 0.0}, confidence=1.0)


PANEL = tuple(range(10))


def structured_skill(body: str, *, version: int = 0) -> S.Skill:
    return S.Skill('searchqa.f', 'f', version, 'lookup', 'scope', body)


class StubExecutor:
    """Deterministic stand-in whose candidate fixes, and acts on, listed tasks.

    Actions and outcomes come from the same predicate, so a task whose runs
    differ is exactly a task the change acts on -- which is what the verifier's
    invariants are checked against.
    """

    protocol_hash = 'stub-executor-v1'
    BASE_ACTIONS = ('search', 'finish')

    def __init__(self, improvement=()):
        self.improvement = set(improvement)

    def _acts(self, skill, task_id):
        return ('open it before placing' in skill.body) and task_id in self.improvement

    def actions(self, skill, task_id):
        return (('open',) + self.BASE_ACTIONS) if self._acts(skill, task_id) \
            else self.BASE_ACTIONS

    def score(self, skill, task_ids, panel_key, role=S.ROLE_EVAL):
        outcomes = []
        for task_id in task_ids:
            success = (task_id % 2 == 0) or self._acts(skill, task_id)
            outcomes.append(S.TaskOutcome(
                task_id=task_id, family_id=skill.family_id, role=role,
                success=success, note='solved' if success else 'unresolved'))
        return V.PanelScore(skill.key, skill.body, tuple(task_ids), tuple(outcomes))

    def records(self, skill, task_ids, panel_key, role=S.ROLE_EVAL):
        return {
            task_id: {
                'task_id': task_id, 'skill_key': skill.key,
                'events': [{'model_text': f'> {action}', 'action': action,
                            'observation': f'after {action}'}
                           for action in self.actions(skill, task_id)],
            }
            for task_id in task_ids
        }


class StubVerifyHost:
    """Records each verifier prompt and returns one fixed verdict."""

    def __init__(self, category='claim_confirmed'):
        self.category = category
        self.prompts = []

    def llm(self, messages, **kwargs):
        self.prompts.append('\n'.join(str(getattr(m, 'content', m)) for m in messages))
        step = None if self.category == 'no_difference' else 1
        return json.dumps({'category': self.category, 'first_difference_step': step,
                           'reason': 'the changed run opens the receptacle first'})


class StubReviewHost:
    """Returns one fixed judgement and keeps the prompt for inspection."""

    def __init__(self, delta=0.6, trigger=0.8, prompts=None):
        self.delta, self.trigger = delta, trigger
        self.prompts = prompts if prompts is not None else []

    def llm(self, messages, **kwargs):
        self.prompts.append('\n'.join(str(getattr(m, 'content', m)) for m in messages))
        return json.dumps({'trigger_probability': self.trigger,
                           'delta_probability': self.delta,
                           'reason': 'the rule fires only when the receptacle is closed'})


class SampledValidatorTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        # The task text is not what these tests exercise, and loading a real
        # benchmark table here would only couple them to a data file.
        patcher = patch.object(F, 'task_text_of',
                               side_effect=lambda cfg, task_id: f'Task {task_id}.')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.routes = SimpleNamespace(groups={'searchqa.f': PANEL}, fingerprint='fp')
        self.base = structured_skill(SS.render(SS.from_sections({
            'procedure': ['Search the named item.'],
            'conditions': ['If the receptacle is exposed, place the object.'],
            'completion_checks': []})))
        self.candidate = structured_skill(SS.render(SS.from_sections({
            'procedure': ['Search the named item.'],
            'conditions': ['If the receptacle is closed, open it before placing the object.'],
            'completion_checks': []})), version=1)
        self.claim = S.Claim('the target receptacle is closed',
                             'open it before placing the object')
        self.prompts = []

    def reviewer(self, delta=0.6):
        host = StubReviewHost(delta=delta, prompts=self.prompts)
        return PairedDeltaReviewer(
            SimpleNamespace(benchmark=SimpleNamespace(name='searchqa', task_file='x')), self.routes,
            V.ScoreCache(self.root / 'predictions.jsonl'), workers=1,
            host_factory=lambda task_id, usage_path: host)

    def validator(self, executor, reviewer, sample_size=4, confidence=0.9):
        return SampledDeltaValidator(
            SimpleNamespace(), self.routes, reviewer, executor,
            sample_size=sample_size, confidence=confidence)

    def test_the_correction_reduces_to_the_measured_sample_when_prediction_is_flat(self):
        validator = self.validator(StubExecutor(improvement=[1, 3, 5, 7, 9]),
                                   self.reviewer())
        result = validator.validate(self.base, self.candidate, self.claim,
                                    'panel', 'sample-key')
        sample = PPI.select_sample(PANEL, 4, 'sample-key')
        self.assertEqual(result.sample_task_ids, sample)
        improved = sum(1 for t in sample if t % 2)
        self.assertAlmostEqual(result.decision.point, improved / len(sample))
        self.assertEqual([row['task_id'] for row in result.rows], list(PANEL))
        self.assertEqual({row['task_id'] for row in result.rows if row['sampled']},
                         set(sample))
        self.assertEqual(result.rows[0]['delta_probability'], 0.6)

    def test_an_optimistic_reviewer_cannot_promote_a_change_that_does_nothing(self):
        # The executor behaves identically under both bodies, so every measured
        # delta is zero while the reviewer claims a large improvement.
        validator = self.validator(StubExecutor(improvement=[]), self.reviewer(delta=0.9))
        result = validator.validate(self.base, self.candidate, self.claim,
                                    'panel', 'sample-key')
        self.assertAlmostEqual(result.decision.point, 0.0)
        self.assertFalse(result.passed)
        self.assertEqual(result.reasons, ('lower_bound_below_zero',))

    def test_the_reviewer_sees_the_changed_rule_the_claim_and_no_trace(self):
        validator = self.validator(StubExecutor(improvement=[1, 3]), self.reviewer())
        validator.validate(self.base, self.candidate, self.claim, 'panel', 'sample-key')
        prompt = self.prompts[0]
        for expected in ('changed_rule', 'If the receptacle is closed, open it before placing',
                         'the target receptacle is closed', 'delta_probability',
                         'exactly ONCE'):
            self.assertIn(expected, prompt)
        self.assertNotIn('"observation"', prompt)
        self.assertNotIn('"success"', prompt)

    def test_a_sampled_validation_serialises_for_the_journal(self):
        validator = self.validator(StubExecutor(improvement=[1, 3]), self.reviewer())
        result = validator.validate(self.base, self.candidate, self.claim,
                                    'panel', 'sample-key')
        payload = result.to_dict()
        self.assertEqual(payload['sample_task_ids'], list(result.sample_task_ids))
        self.assertIn('accepted', payload['decision'])
        self.assertEqual(len(payload['rows']), len(PANEL))


class SampledBatchJournalTests(unittest.TestCase):
    """The journal a sampled batch leaves behind must replay without the models."""

    def setUp(self):
        patcher = patch.object(F, 'task_text_of',
                               side_effect=lambda cfg, task_id: f'Task {task_id}.')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = Path(tempfile.mkdtemp())
        self.routes = SimpleNamespace(groups={'searchqa.f': PANEL}, fingerprint='fp')
        self.base = structured_skill(SS.render(SS.from_sections({
            'procedure': ['Search the named item.'],
            'conditions': ['If the receptacle is exposed, place the object.'],
            'completion_checks': []})))
        self.candidate = structured_skill(SS.render(SS.from_sections({
            'procedure': ['Search the named item.'],
            'conditions': ['If the receptacle is closed, open it before placing the object.'],
            'completion_checks': []})), version=1)
        self.claim = S.Claim('the target receptacle is closed',
                             'open it before placing the object')
        self.planner = _PlannerHost(self.claim)

    def journal(self, *, delta=0.6, improvement=(1, 3, 5, 7, 9), sample_size=4,
                verifier=None):
        reviewer = PairedDeltaReviewer(
            SimpleNamespace(benchmark=SimpleNamespace(name='searchqa', task_file='x')),
            self.routes, V.ScoreCache(self.root / 'predictions.jsonl'), workers=1,
            host_factory=lambda task_id, usage_path: StubReviewHost(delta=delta))
        validator = SampledDeltaValidator(
            SimpleNamespace(), self.routes, reviewer, StubExecutor(improvement),
            sample_size=sample_size, confidence=0.9, verifier=verifier)
        runner = UP.SkillPatchRunner(
            SkillEditor(self.planner, skill_edit_mode='structured'), None,
            self.root / 'l2_proposals', acceptance_mode='sampled',
            sampled_validator=validator, single_candidate=True)
        return runner.run(self.base, [fake_experience()], 1, l2_review_workers=1)

    def test_a_measured_improvement_is_accepted_and_the_journal_replays(self):
        result = self.journal()
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.record['outcome'], 'review_approved')
        self.assertEqual(result.record['selection_method'], 'sampled_paired_delta')
        acceptance = result.record['acceptance']
        self.assertEqual(acceptance['mode'], 'sampled')
        self.assertGreater(acceptance['executions'], 0)
        self.assertEqual(acceptance['jev_requests'], 0)
        claim = result.record['hypotheses'][0]['claim']
        self.assertEqual(claim['claim_id'], self.claim.claim_id)
        self.assertEqual(result.record['proposals'][0]['claim'], claim)
        audit(self.root, result.record)

    def test_a_change_the_sample_contradicts_is_rejected(self):
        result = self.journal(delta=0.9, improvement=())
        self.assertIsNone(result.candidate)
        self.assertEqual(result.record['outcome'], 'hold')
        audit(self.root, result.record)

    def test_the_audit_rejects_a_sample_that_the_key_does_not_select(self):
        result = self.journal()
        batch = json.loads(json.dumps(result.record))
        row = batch['acceptance']['candidates'][0]['result']
        row['sample_task_ids'] = [t for t in PANEL if t not in row['sample_task_ids']][:4]
        with self.assertRaises(AuditFailure):
            audit(self.root, batch)

    def test_the_audit_rejects_a_decision_the_rows_contradict(self):
        result = self.journal()
        batch = json.loads(json.dumps(result.record))
        batch['acceptance']['candidates'][0]['result']['decision']['accepted'] = False
        with self.assertRaises(AuditFailure):
            audit(self.root, batch)

    def test_a_later_round_learns_only_from_journaled_earlier_ones(self):
        first = self.journal()
        batches = self.root / 'l2_batches'
        batches.mkdir(exist_ok=True)
        (batches / 'r1.json').write_text(json.dumps(dict(first.record, round=1)))

        changes = LED.read_changes(self.root, before_round=2)
        self.assertEqual(len(changes), 1)
        # The scripted Planner adds a Conditions rule, which is the type the
        # ledger must report and the history must be grouped by.
        self.assertEqual(changes[0].change_type(), 'conditions/add')
        memory = MEM.PlannerMemory(changes).render()
        self.assertIn('conditions/add', memory)

        batch = {'round': 2, 'planner_memory': memory, 'reviewer_memory_version': 1}
        SA.audit_memories(self.root, batch, CONFIG)
        # The same ledger read without the round filter would describe a panel
        # the next round has not seen yet, so the audit must notice.
        with self.assertRaises(AuditFailure):
            SA.audit_memories(self.root, dict(batch, reviewer_memory_version=0), CONFIG)

    def test_the_audit_rejects_a_batch_without_claims(self):
        result = self.journal()
        batch = json.loads(json.dumps(result.record))
        batch['hypotheses'][0].pop('claim')
        with self.assertRaises(AuditFailure):
            audit(self.root, batch)


class VerificationTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(F, 'task_text_of',
                               side_effect=lambda cfg, task_id: f'Task {task_id}.')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = Path(tempfile.mkdtemp())
        self.routes = SimpleNamespace(groups={'searchqa.f': PANEL}, fingerprint='fp')
        self.cfg = SimpleNamespace(benchmark=SimpleNamespace(name='searchqa', task_file='x'))
        self.base = structured_skill(SS.render(SS.from_sections({
            'procedure': ['Search the named item.'],
            'conditions': ['If the receptacle is exposed, place the object.'],
            'completion_checks': []})))
        self.candidate = structured_skill(SS.render(SS.from_sections({
            'procedure': ['Search the named item.'],
            'conditions': ['If the receptacle is closed, open it before placing the object.'],
            'completion_checks': []})), version=1)
        self.claim = S.Claim('the target receptacle is closed',
                             'open it before placing the object')

    def verifier(self, host):
        return TrajectoryVerifier(self.cfg, V.ScoreCache(self.root / 'verifications.jsonl'),
                                  workers=1,
                                  host_factory=lambda task_id, usage_path: host)

    def validator(self, **kwargs):
        reviewer = PairedDeltaReviewer(
            self.cfg, self.routes, V.ScoreCache(self.root / 'predictions.jsonl'), workers=1,
            host_factory=lambda task_id, usage_path: StubReviewHost(delta=0.6))
        return SampledDeltaValidator(
            self.cfg, self.routes, reviewer, StubExecutor(improvement=[1, 3, 5, 7, 9]),
            sample_size=4, confidence=0.9, **kwargs)

    def test_the_verifier_reads_both_executions_and_caches_its_verdict(self):
        host = StubVerifyHost(category='claim_not_confirmed')
        verifier = self.verifier(host)
        base = ({'model_text': 'Action 1: Search[maker]', 'observation': 'Toyota makes it'},
                {'model_text': 'Action 2: Finish[Honda]', 'observation': 'Answer is INCORRECT'})
        candidate = ({'model_text': 'Action 1: Search[Prius maker]', 'observation': 'D1'},
                     {'model_text': 'Action 2: Finish[Toyota]',
                      'observation': 'Answer is CORRECT'})
        changed = {'section': 'conditions', 'rule_id': 'C1',
                   'before': 'If exposed, place.', 'after': 'If closed, open then place.'}
        first = verifier.verify(1, changed, self.claim, base, candidate, 'panel')
        second = verifier.verify(1, changed, self.claim, base, candidate, 'panel')
        self.assertEqual((first['category'], first['first_difference_step']),
                         ('claim_not_confirmed', 1))
        self.assertEqual(len(host.prompts), 1)
        self.assertEqual(second, first)
        prompt = host.prompts[0]
        for expected in ('execution_without_change', 'execution_with_change',
                         'Search[Prius maker]', 'Toyota makes it', 'deliberately withheld'):
            self.assertIn(expected, prompt)
        # The last observation reports the outcome, so neither run shows it.
        self.assertNotIn('CORRECT', prompt)

    def test_the_trajectory_view_withholds_only_the_final_observation(self):
        # Long observations (three search documents, an admissible-command menu)
        # reach the verifier whole: only the outcome-bearing last one is dropped.
        page = 'D1 ' + 'x' * 5000 + ' Admissible actions: open fridge 1'
        events = ({'model_text': 'a', 'observation': page},
                  {'model_text': 'b', 'observation': 'You won!'})
        self.assertEqual(trajectory_view(events),
                         [{'executor': 'a', 'observation': page},
                          {'executor': 'b', 'observation': ''}])
        self.assertEqual(trajectory_view(()), [])

    def test_an_unknown_category_is_rejected(self):
        # The parser is checked directly: the repair budget would otherwise make
        # this test sleep through eight backoff waits.
        verifier = self.verifier(StubVerifyHost())
        def parse(category, step, reason='because'):
            return verifier._parse_response(json.dumps(
                {'category': category, 'first_difference_step': step, 'reason': reason}))

        for bad in (('looks_good', 1), ('unrelated', 1, ''), ('unrelated', 0),
                    ('no_difference', 2), ('unrelated', True)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse(*bad)
        # Length is not a format error: a long reason must not exhaust retries.
        self.assertEqual(len(parse('unrelated', 1, 'r' * 5000)['reason']), 5000)
        self.assertEqual(parse('unrelated', 3)['first_difference_step'], 3)
        self.assertIsNone(parse('no_difference', None)['first_difference_step'])

    def test_verification_covers_exactly_the_sampled_tasks(self):
        host = StubVerifyHost()
        validator = self.validator(verifier=self.verifier(host))
        result = validator.validate(self.base, self.candidate, self.claim,
                                    'panel', 'sample-key')
        self.assertTrue(result.verification_enabled)
        verified = {row['task_id'] for row in result.rows if row['verification'] is not None}
        self.assertEqual(verified, set(result.sample_task_ids))
        self.assertEqual(len(host.prompts), len(result.sample_task_ids))
        self.assertTrue(all(row['verification']['category'] in CATEGORIES
                            for row in result.rows if row['verification']))

    def test_verification_off_records_no_verdicts_and_never_calls_the_judge(self):
        host = StubVerifyHost()
        validator = self.validator()
        result = validator.validate(self.base, self.candidate, self.claim,
                                    'panel', 'sample-key')
        self.assertFalse(result.verification_enabled)
        self.assertEqual(host.prompts, [])
        self.assertTrue(all(row['verification'] is None for row in result.rows))

    def test_the_audit_requires_a_verdict_for_every_sampled_task(self):
        journals = SampledBatchJournalTests('journal')
        journals.setUp()
        record = journals.journal(verifier=self.verifier(StubVerifyHost())).record
        audit(journals.root, record)
        stripped = json.loads(json.dumps(record))
        rows = stripped['acceptance']['candidates'][0]['result']['rows']
        target = next(row for row in rows if row['verification'] is not None)
        target['verification'] = None
        with self.assertRaises(AuditFailure):
            audit(journals.root, stripped)

def change_row(task_id, predicted, measured, *, sampled=True, category=None):
    return {'task_id': task_id, 'delta_probability': predicted,
            'measured_delta': measured, 'sampled': sampled,
            'verification': ({'category': category, 'first_difference_step': None,
                              'reason': 'because'} if category else None)}


def ledger_journal(round_index, candidate_id, *, section='conditions', op='ADD',
                   accepted=True, point=0.4, lower=0.1, rows=None,
                   trigger='the target receptacle is closed'):
    return {
        'round': round_index, 'batch_id': f'b{round_index}-{candidate_id}',
        'family_id': 'f', 'task_ids': [0, 1],
        'hypotheses': [{'claim': {'trigger': trigger, 'action_change': 'open it'}}],
        'proposals': [{'claim': {'trigger': trigger, 'action_change': 'open it'},
                       'edit': {'candidate': {'candidate_id': candidate_id,
                                              'edits': [{'op': op, 'section': section,
                                                         'target_id': None,
                                                         'text': 'rule'}]}}}],
        'acceptance': {'mode': 'sampled', 'scope': 'val',
                       'candidates': [{'candidate_id': candidate_id,
                                       'result': {'decision': {'accepted': accepted,
                                                               'point': point,
                                                               'lower': lower},
                                                  'rows': rows if rows is not None else [
                                                      change_row(0, 0.6, 1.0,
                                                                 category='claim_confirmed'),
                                                      change_row(1, 0.6, 0.0)]}}]},
    }


class MemoryTests(unittest.TestCase):
    def root_with(self, journals):
        root = Path(tempfile.mkdtemp())
        (root / 'l2_batches').mkdir()
        for index, journal in enumerate(journals):
            (root / 'l2_batches' / f'{index}.json').write_text(json.dumps(journal))
        return root

    def test_the_ledger_is_read_oldest_first_and_only_before_a_round(self):
        root = self.root_with([ledger_journal(2, 'c2'), ledger_journal(1, 'c1'),
                               ledger_journal(3, 'c3')])
        changes = LED.read_changes(root, before_round=2)
        self.assertEqual([change.candidate_id for change in changes], ['c1'])
        self.assertEqual([change.candidate_id for change in LED.read_changes(root)],
                         ['c1', 'c2', 'c3'])

    def test_a_change_is_classified_by_what_the_sample_measured(self):
        root = self.root_with([
            ledger_journal(1, 'c1', accepted=True, rows=[change_row(0, 0.6, 1.0),
                                                         change_row(1, 0.6, 0.0)]),
            ledger_journal(1, 'c2', accepted=False, rows=[change_row(0, 0.6, 0.0),
                                                          change_row(1, 0.6, 0.0)]),
            ledger_journal(1, 'c3', accepted=False, rows=[change_row(0, 0.6, 0.5),
                                                          change_row(1, 0.6, 0.3)]),
        ])
        verdicts = {change.candidate_id: MEM.verdict(change)
                    for change in LED.read_changes(root)}
        self.assertEqual(verdicts, {'c1': MEM.VERDICT_EFFECTIVE,
                                    'c2': MEM.VERDICT_NO_EFFECT,
                                    'c3': MEM.VERDICT_INSUFFICIENT})

    def test_the_planner_memory_names_no_task_and_only_carries_counts(self):
        root = self.root_with([
            ledger_journal(1, 'c1', rows=[change_row(0, 0.6, 1.0, category='claim_confirmed'),
                                          change_row(1, 0.6, 0.0)]),
            ledger_journal(1, 'c2', section='procedure', op='EDIT', accepted=False,
                           rows=[change_row(0, 0.6, 0.0), change_row(1, 0.6, 0.0)]),
        ])
        text = MEM.PlannerMemory(LED.read_changes(root)).render()
        self.assertIn('conditions/add', text)
        self.assertIn('procedure/replace', text)
        self.assertIn('no measured effect', text)
        self.assertNotIn('task 0', text)
        self.assertNotIn('Task 0.', text)
        for forbidden in ('"', 't0:e1', 'question', 'receptacle'):
            self.assertNotIn(forbidden, text)

    def test_an_empty_ledger_renders_no_memory(self):
        self.assertEqual(MEM.PlannerMemory(()).render(), '')

    def reviewer_memory(self, root):
        return MEM.ReviewerMemory.build(
            LED.read_changes(root), task_text=lambda task_id: f'Task {task_id}.')

    def test_the_reviewer_memory_keeps_over_and_under_estimates_as_cases(self):
        root = self.root_with([
            ledger_journal(1, 'c1', rows=[change_row(0, 0.8, 0.0, category='claim_confirmed'),
                                          change_row(1, 0.0, 1.0)]),
        ])
        memory = self.reviewer_memory(root)
        kinds = {case.kind for case in memory.cases}
        self.assertEqual(kinds, {MEM.OVER_ESTIMATED, MEM.UNDER_ESTIMATED})
        block = memory.block_for(section='conditions', op='add', task_id=99,
                                 exclude_candidate_id='other')
        self.assertIn('over_estimated', block)
        self.assertIn('under_estimated', block)
        self.assertIn('Task 0.', block)
        self.assertIn('verifier: claim_confirmed', block)

    def test_retrieval_excludes_the_task_and_the_proposal_under_judgement(self):
        root = self.root_with([
            ledger_journal(1, 'c1', rows=[change_row(0, 0.8, 0.0, category='claim_confirmed'),
                                          change_row(1, 0.8, 0.0, category='claim_confirmed')]),
        ])
        memory = self.reviewer_memory(root)
        self.assertEqual(memory.block_for(section='conditions', op='add', task_id=0,
                                          exclude_candidate_id='other')
                         .count('task "'), 1)
        self.assertEqual(memory.block_for(section='conditions', op='add', task_id=0,
                                          exclude_candidate_id='c1'), '')
        self.assertEqual(memory.block_for(section='other', op='add', task_id=0,
                                          exclude_candidate_id='other'), '')

    def test_the_reviewer_prompt_carries_only_the_current_task_block(self):
        prompts = []
        reviewer = PairedDeltaReviewer(
            SimpleNamespace(benchmark=SimpleNamespace(name='searchqa', task_file='x')),
            SimpleNamespace(groups={'searchqa.f': (0, 1)}, fingerprint='fp'),
            V.ScoreCache(Path(tempfile.mkdtemp()) / 'p.jsonl'), workers=1,
            host_factory=lambda task_id, usage_path: StubReviewHost(prompts=prompts))
        base = structured_skill(SS.render(SS.from_sections({
            'procedure': ['Search.'], 'conditions': ['If exposed, place.'],
            'completion_checks': []})))
        candidate = structured_skill(SS.render(SS.from_sections({
            'procedure': ['Search.'], 'conditions': ['If closed, open then place.'],
            'completion_checks': []})), version=1)
        with patch.object(F, 'task_text_of', side_effect=lambda cfg, t: f'Task {t}.'):
            reviewer.predict(base, candidate, S.Claim('closed', 'open then place'),
                             (0, 1), 'panel',
                             memory_blocks={0: 'REVIEWER MEMORY for zero',
                                            1: 'REVIEWER MEMORY for one'})
        by_task = {prompt: index for index, prompt in enumerate(prompts)}
        zero = next(prompt for prompt in prompts if '"Task 0."' in prompt)
        one = next(prompt for prompt in prompts if '"Task 1."' in prompt)
        self.assertIn('REVIEWER MEMORY for zero', zero)
        self.assertNotIn('REVIEWER MEMORY for one', zero)
        self.assertIn('REVIEWER MEMORY for one', one)
        self.assertEqual(len(by_task), len(prompts))

    def test_the_audit_recomputes_what_the_planner_memory_held(self):
        root = self.root_with([ledger_journal(1, 'c1')])
        changes = LED.read_changes(root)
        batch = {'round': 2, 'planner_memory': MEM.PlannerMemory(changes).render(),
                 'reviewer_memory_version': 1}
        SA.audit_memories(root, batch, CONFIG)
        leaked = dict(batch, planner_memory=batch['planner_memory'] + '\nquestion 0')
        with self.assertRaises(AuditFailure):
            SA.audit_memories(root, leaked, CONFIG)
        with self.assertRaises(AuditFailure):
            SA.audit_memories(root, dict(batch, reviewer_memory_version=2), CONFIG)

    def test_claim_statistics_count_only_rules_the_verifier_implicated(self):
        rows = [change_row(0, 0.6, 1.0, category='claim_confirmed'),
                change_row(1, 0.6, 0.0, category='unrelated'),
                change_row(2, 0.6, 0.0)]
        root = self.root_with([ledger_journal(1, 'c1', rows=rows)])
        change = LED.read_changes(root)[0]
        # A difference the verifier calls unrelated is executor drift, not a firing.
        self.assertEqual(MEM.claim_counts(change), (3, 1, 1))
        self.assertIn('rule fired 1/3', MEM.PlannerMemory((change,)).render())
        self.assertNotIn('rule fired', MEM.PlannerMemory(
            (change,), claims_verified=False).render())

    def test_reviewer_metrics_compare_against_predicting_no_effect(self):
        rows = [change_row(0, 0.5, 1.0), change_row(1, 0.0, 0.0)]
        root = self.root_with([ledger_journal(1, 'c1', accepted=True, rows=rows)])
        metrics = LED.reviewer_metrics(LED.read_changes(root))
        self.assertEqual(metrics['sampled_pairs'], 2)
        self.assertAlmostEqual(metrics['delta_brier'], 0.125)
        self.assertAlmostEqual(metrics['delta_brier_zero_baseline'], 0.5)
        self.assertAlmostEqual(metrics['delta_brier_skill'], 0.75)
        self.assertEqual((metrics['accepted'], metrics['false_accepts']), (1, 0))


class SampledCampaignTests(unittest.TestCase):
    """A campaign must freeze the whole protocol before any stage runs."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        overlay = tmp / 'overlay'
        overlay.mkdir()
        config = tmp / 'alfred.pddl.yaml'
        config.write_text('{}')
        patcher = patch.dict('os.environ', {
            'EXPE_LLM_MODEL': 'stub-model',
            'EXPE_LLM_BASE_URL': 'http://127.0.0.1:1/v1',
            'ALFWORLD_PYTHON': '/usr/bin/true',
            'EXPE_CAMPAIGN_OVERLAY': str(overlay),
            'ALFWORLD_DATA': str(overlay),
            'ALFWORLD_CONFIG': str(config),
            'ALFWORLD_BENCH_SRC': str(overlay),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.inputs = tmp / 'inputs'
        self.inputs.mkdir()
        tasks = [{'task': f'Question {i}?', 'env_kwargs': {'key': f'answer {i}'}}
                 for i in range(8)]
        assignment = {str(i): ('train' if i < 4 else 'val' if i < 6 else 'test')
                      for i in range(8)}
        for name in ('searchqa', 'alfworld'):
            (self.inputs / f'{name}-tasks.json').write_text(json.dumps(tasks))
            (self.inputs / f'{name}-split.json').write_text(
                json.dumps({'assignment': assignment}))
        self.root = tmp / 'campaign'

    def prepare(self, **overrides):
        options = dict(skill_edit_mode='structured', acceptance_mode='sampled',
                       candidate_count=1, single_candidate=True,
                       acceptance_sample_size=4, claim_verification='on',
                       planner_memory_mode='aggregate', reviewer_memory_mode='cases')
        options.update(overrides)
        return C.prepare(self.root, self.inputs, **options)

    def test_the_manifest_freezes_every_switch_and_the_role_map(self):
        self.prepare()
        manifest = json.loads((self.root / 'manifest.json').read_text())
        self.assertEqual(manifest['acceptance_mode'], 'sampled')
        self.assertEqual((manifest['acceptance_sample_size'],
                          manifest['acceptance_confidence']), (4, 0.9))
        self.assertEqual(manifest['claim_verification'], 'on')
        self.assertEqual((manifest['planner_memory_mode'],
                          manifest['reviewer_memory_mode']), ('aggregate', 'cases'))
        self.assertEqual(set(manifest['models']), set(C.ROLES))
        self.assertIn('l2_verifier', manifest['models'])

    def test_the_stage_arguments_carry_the_protocol(self):
        self.prepare()
        args = C.stage_args(self.root, 'preflight', 'searchqa', 'evolve-1')
        for flag, value in (('--acceptance-mode', 'sampled'),
                            ('--acceptance-sample-size', '4'),
                            ('--acceptance-confidence', '0.9'),
                            ('--claim-verification', 'on'),
                            ('--planner-memory-mode', 'aggregate'),
                            ('--reviewer-memory-mode', 'cases')):
            self.assertEqual(args[args.index(flag) + 1], value, flag)
        self.assertIn('--l2-verifier-model', args)
        self.assertNotIn('--predicted-review-scope', args)

    def test_a_tampered_manifest_is_rejected_before_any_stage(self):
        self.prepare()
        path = self.root / 'manifest.json'
        manifest = json.loads(path.read_text())
        manifest['acceptance_sample_size'] = 1
        path.write_text(json.dumps(manifest))
        with self.assertRaises(FrozenProtocolChanged):
            C.verify(self.root)

    def test_the_sampled_protocol_requires_structured_editing(self):
        with self.assertRaises(InvalidInput):
            self.prepare(skill_edit_mode='rewrite')


if __name__ == '__main__':
    unittest.main()
