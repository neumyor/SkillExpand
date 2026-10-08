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
from skillexpand.evaluation.claim_check import CATEGORIES, TrajectoryVerifier
from skillexpand.evaluation.divergence import action_sequence, first_divergence
from skillexpand.evaluation.sampled_validation import SampledDeltaValidator
from skillexpand.runtime import agent_factory as F
from skillexpand.persistence.io import AuditFailure
from skillexpand.l2.audit import audit_sampled_batch

EVIDENCE_ID = 't1:e1'


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
        self.assertNotIn('falsifiable claim', host.prompt)

        editor.plan(base, [experience], 1, batch_patterns=(), claim_required=True)
        self.assertIn('falsifiable claim', host.prompt)
        self.assertIn('action_change', host.prompt)

    def test_sampled_requires_structured_editing(self):
        SM.validate_protocol('sampled', 'structured')
        SM.validate_protocol('predicted', 'rewrite')
        with self.assertRaises(ValueError):
            SM.validate_protocol('sampled', 'rewrite')


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

    Actions and outcomes come from the same predicate, so a task that diverges
    is exactly a task the change acts on -- which is what the verifier's
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
                'events': [{'action': action, 'observation': f'after {action}'}
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
        return json.dumps({'category': self.category,
                           'reason': 'the change adds the open step at the divergence'})


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

    def journal(self, *, delta=0.6, improvement=(1, 3, 5, 7, 9), sample_size=4):
        reviewer = PairedDeltaReviewer(
            SimpleNamespace(benchmark=SimpleNamespace(name='searchqa', task_file='x')),
            self.routes, V.ScoreCache(self.root / 'predictions.jsonl'), workers=1,
            host_factory=lambda task_id, usage_path: StubReviewHost(delta=delta))
        validator = SampledDeltaValidator(
            SimpleNamespace(), self.routes, reviewer, StubExecutor(improvement),
            sample_size=sample_size, confidence=0.9)
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
        audit_sampled_batch(result.record)

    def test_a_change_the_sample_contradicts_is_rejected(self):
        result = self.journal(delta=0.9, improvement=())
        self.assertIsNone(result.candidate)
        self.assertEqual(result.record['outcome'], 'hold')
        audit_sampled_batch(result.record)

    def test_the_audit_rejects_a_sample_that_the_key_does_not_select(self):
        result = self.journal()
        batch = json.loads(json.dumps(result.record))
        row = batch['acceptance']['candidates'][0]['result']
        row['sample_task_ids'] = [t for t in PANEL if t not in row['sample_task_ids']][:4]
        with self.assertRaises(AuditFailure):
            audit_sampled_batch(batch)

    def test_the_audit_rejects_a_decision_the_rows_contradict(self):
        result = self.journal()
        batch = json.loads(json.dumps(result.record))
        batch['acceptance']['candidates'][0]['result']['decision']['accepted'] = False
        with self.assertRaises(AuditFailure):
            audit_sampled_batch(batch)

    def test_the_audit_rejects_a_batch_without_claims(self):
        result = self.journal()
        batch = json.loads(json.dumps(result.record))
        batch['hypotheses'][0].pop('claim')
        with self.assertRaises(AuditFailure):
            audit_sampled_batch(batch)


class DivergenceTests(unittest.TestCase):
    def events(self, actions):
        return tuple({'action': action, 'observation': f'after {action}'}
                     for action in actions)

    def test_identical_actions_have_no_divergence(self):
        self.assertIsNone(first_divergence(self.events(['a', 'b']), self.events(['a', 'b'])))
        self.assertEqual(action_sequence(self.events(['a', 'b'])), ('a', 'b'))

    def test_the_first_differing_step_is_reported_with_its_context(self):
        base = self.events(['search', 'open', 'finish'])
        candidate = self.events(['search', 'open', 'take', 'finish'])
        divergence = first_divergence(base, candidate)
        self.assertEqual(divergence.step, 3)
        self.assertEqual(divergence.base_action, 'finish')
        self.assertEqual(divergence.candidate_action, 'take')
        self.assertEqual(divergence.prefix_actions, ('search', 'open'))
        self.assertEqual(divergence.context_observation, 'after open')
        self.assertEqual((divergence.base_steps, divergence.candidate_steps), (3, 4))

    def test_a_run_that_stops_earlier_is_a_divergence(self):
        divergence = first_divergence(self.events(['a', 'b']), self.events(['a']))
        self.assertEqual(divergence.step, 2)
        self.assertIsNone(divergence.candidate_action)
        self.assertEqual(divergence.base_action, 'b')

    def test_only_executed_actions_align_the_two_runs(self):
        base = ({'action': 'a'}, {'observation': 'narration without an action'},
                {'action': 'b'})
        candidate = ({'action': 'a'}, {'action': 'b'})
        self.assertIsNone(first_divergence(base, candidate))


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

    def test_the_verifier_parses_and_caches_its_verdict(self):
        host = StubVerifyHost(category='claim_not_confirmed')
        verifier = self.verifier(host)
        divergence = first_divergence(
            ({'action': 'a'},), ({'action': 'b'},))
        changed = {'section': 'conditions', 'rule_id': 'C1',
                   'before': 'If exposed, place.', 'after': 'If closed, open then place.'}
        first = verifier.verify(1, changed, self.claim, divergence, 'panel')
        second = verifier.verify(1, changed, self.claim, divergence, 'panel')
        self.assertEqual(first['category'], 'claim_not_confirmed')
        self.assertEqual(len(host.prompts), 1)
        self.assertEqual(second['category'], first['category'])
        for expected in ('execution_difference', 'changed_rule', 'deliberately withheld',
                         'action_without_change'):
            self.assertIn(expected, host.prompts[0])

    def test_an_unknown_category_is_rejected(self):
        # The parser is checked directly: the repair budget would otherwise make
        # this test sleep through eight backoff waits.
        verifier = self.verifier(StubVerifyHost())
        with self.assertRaises(ValueError):
            verifier._parse_response(json.dumps({'category': 'looks_good',
                                                 'reason': 'fine'}))
        with self.assertRaises(ValueError):
            verifier._parse_response(json.dumps({'category': 'unrelated',
                                                 'reason': ''}))
        self.assertEqual(
            verifier._parse_response(json.dumps({'category': 'unrelated',
                                                 'reason': 'the step is unrelated'}))
            ['category'], 'unrelated')

    def test_verification_covers_exactly_the_observed_differences(self):
        host = StubVerifyHost()
        validator = self.validator(verifier=self.verifier(host))
        result = validator.validate(self.base, self.candidate, self.claim,
                                    'panel', 'sample-key')
        executed = set(result.sample_task_ids)
        diverged = {task_id for task_id in executed if task_id in {1, 3, 5, 7, 9}}
        self.assertTrue(result.verification_enabled)
        self.assertEqual({row['task_id'] for row in result.rows
                          if row['verification'] is not None}, diverged)
        self.assertEqual(len(host.prompts), len(diverged))
        for row in result.rows:
            if not row['sampled']:
                self.assertIsNone(row['divergence'])
                self.assertIsNone(row['verification'])
            elif row['task_id'] in diverged:
                self.assertEqual(row['divergence']['diverged_at_step'], 1)
                self.assertIn(row['verification']['category'], CATEGORIES)
            else:
                self.assertIsNone(row['divergence'])
                self.assertIsNone(row['verification'])

    def test_verification_off_records_no_verdicts_and_never_calls_the_judge(self):
        host = StubVerifyHost()
        validator = self.validator()
        result = validator.validate(self.base, self.candidate, self.claim,
                                    'panel', 'sample-key')
        self.assertFalse(result.verification_enabled)
        self.assertEqual(host.prompts, [])
        self.assertTrue(all(row['verification'] is None for row in result.rows))

    def test_the_audit_requires_a_verdict_for_every_observed_difference(self):
        validator = self.validator(verifier=self.verifier(StubVerifyHost()))
        result = validator.validate(self.base, self.candidate, self.claim,
                                    'panel', 'sample-key').to_dict()
        batch = {
            'base_skill_key': self.base.key,
            'hypotheses': [dict(hypothesis(), claim=self.claim.to_dict())],
            'proposals': [{'claim': self.claim.to_dict(),
                           'edit': {'candidate': {'candidate_id': 'c1'}}}],
            'acceptance': {'mode': 'sampled', 'scope': 'val',
                           'task_ids': list(PANEL), 'sample_size': 4,
                           'confidence': 0.9, 'executions': 4,
                           'predicted_requests': len(PANEL),
                           'candidates': [{'candidate_id': 'c1', 'result': result}]},
        }
        audit_sampled_batch(batch)
        stripped = json.loads(json.dumps(batch))
        rows = stripped['acceptance']['candidates'][0]['result']['rows']
        target = next(row for row in rows if row['verification'] is not None)
        target['verification'] = None
        with self.assertRaises(AuditFailure):
            audit_sampled_batch(stripped)


if __name__ == '__main__':
    unittest.main()
