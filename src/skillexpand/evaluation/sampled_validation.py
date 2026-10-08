"""Accept a candidate from a sampled measurement corrected by the prediction.

The Reviewer predicts the per-task delta over the whole frozen val panel; a
random sample of that panel is then executed for both arms, and the measured
error on the sample corrects the panel-wide prediction
(:mod:`skillexpand.evaluation.ppi`).  The candidate is installed only when the
one-sided lower confidence bound of the corrected delta exceeds zero.

Two properties make this the whole reason the protocol is safe to run:

* the estimate stays unbiased whatever the Reviewer's bias is, so no amount of
  optimism in the prediction can promote a candidate the sample contradicts;
* the reviewer's precision is what sets the interval width, so a better Reviewer
  needs fewer executed tasks for the same confidence.

This is a separate path from the empirical and predicted-val scorers: it is the
only one that both predicts and measures, and its result type records the
per-task prediction, the sampled measurements and the interval so that an
offline audit can recompute the decision from the journal alone.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from skillexpand import schema as S
from skillexpand.evaluation import ppi as PPI
from skillexpand.evaluation.delta_review import change_view
from skillexpand.evaluation.divergence import first_divergence
from skillexpand.reliability.errors import InvalidInput

#: Below this many executed pairs the interval cannot be read as evidence.
MIN_SAMPLE_SIZE = 2


@dataclass(frozen=True)
class SampledValidation:
    """One sampled-acceptance decision, with everything needed to replay it."""

    skill_id: str
    panel_key: str
    sample_key: str
    base_skill_key: str
    candidate_skill_key: str
    claim_id: str
    panel_task_ids: Tuple[int, ...]
    sample_task_ids: Tuple[int, ...]
    decision: PPI.SampleDecision
    arms: Tuple[S.ArmEvaluation, ...]
    rows: Tuple[Dict[str, Any], ...]
    #: Whether an independent verifier attributed the divergences.  Recorded so
    #: the audit can tell "no divergence found" from "verification disabled".
    verification_enabled: bool = False

    @property
    def passed(self) -> bool:
        return self.decision.accepted

    @property
    def task_ids(self) -> Tuple[int, ...]:
        return self.panel_task_ids

    @property
    def reasons(self) -> Tuple[str, ...]:
        return () if self.passed else (self.decision.reason,)

    def metrics(self) -> Dict[str, Optional[float]]:
        return dict(self.decision.metrics(),
                    mean_predicted_delta=self.mean_predicted_delta,
                    mean_trigger_probability=self.mean_trigger_probability,
                    reviewer_from_cache=float(self.reviewer_from_cache),
                    reviewer_measured=float(self.reviewer_measured))

    @property
    def mean_predicted_delta(self) -> Optional[float]:
        if not self.rows:
            return None
        return sum(float(row['delta_probability']) for row in self.rows) / len(self.rows)

    @property
    def mean_trigger_probability(self) -> Optional[float]:
        if not self.rows:
            return None
        return sum(float(row['trigger_probability']) for row in self.rows) / len(self.rows)

    @property
    def reviewer_from_cache(self) -> int:
        return sum(1 for row in self.rows if row.get('from_cache'))

    @property
    def reviewer_measured(self) -> int:
        return sum(1 for row in self.rows if not row.get('from_cache'))

    @property
    def sample_outcomes(self) -> Dict[int, float]:
        """Measured candidate-minus-base success on each executed task."""
        rates = {}
        for arm in self.arms:
            rates[arm.arm_id] = arm.by_task_rate()
        base = rates.get(S.ARM_BASE, {})
        candidate = rates.get(S.ARM_CANDIDATE, {})
        shared = sorted(set(base) & set(candidate))
        if set(shared) != set(self.sample_task_ids):
            raise InvalidInput('Sampled arms do not cover the executed sample')
        return {t: candidate[t] - base[t] for t in shared}

    def to_dict(self) -> Dict[str, Any]:
        return S.to_dict(self)


class SampledDeltaValidator:
    """Corrections from a random val sample; the decision rule is fixed here."""

    def __init__(self, cfg, routes, reviewer, executor, sample_size: int = 16,
                 confidence: float = 0.9, verifier: Optional[Any] = None,
                 reviewer_memory: Optional[Any] = None):
        if int(sample_size) < MIN_SAMPLE_SIZE:
            raise InvalidInput(
                f'acceptance sample size must be at least {MIN_SAMPLE_SIZE}: '
                'a single executed pair carries no measurable spread')
        if not 0.0 < float(confidence) < 1.0:
            raise InvalidInput('acceptance confidence must lie strictly between 0 and 1')
        self.cfg = cfg
        self.routes = routes
        self.reviewer = reviewer
        self.executor = executor
        self.sample_size = int(sample_size)
        self.confidence = float(confidence)
        self.verifier = verifier
        # Duck-typed on purpose: the memory is built by the layer that owns the
        # journals, and this module only asks it for the cases of one change type.
        self.reviewer_memory = reviewer_memory
        self.protocol_hash = S.content_hash({
            'protocol': 'sampled-delta-acceptance',
            'reviewer': reviewer.protocol_hash,
            'executor': executor.protocol_hash,
            'sample_size': self.sample_size,
            'confidence': self.confidence,
            'verifier': getattr(verifier, 'protocol_hash', None),
            'reviewer_memory': (
                None if reviewer_memory is None
                else getattr(reviewer_memory, 'version', 0)),
        })

    def validate(self, base_skill: S.Skill, candidate_skill: S.Skill, claim: S.Claim,
                 panel_key: str, sample_key: str,
                 exclude_candidate_id: str = '') -> SampledValidation:
        if base_skill.skill_id != candidate_skill.skill_id:
            raise InvalidInput('Both arms must belong to the same Skill')
        panel = tuple(sorted(int(t) for t in self.routes.groups[base_skill.skill_id]))
        if not panel:
            raise InvalidInput(
                f'Val panel is empty for {base_skill.skill_id}; sampled acceptance '
                'has nothing to sample')

        sample = PPI.select_sample(panel, self.sample_size, sample_key)
        blocks = {}
        if self.reviewer_memory is not None:
            changed = change_view(base_skill, candidate_skill)
            blocks = {
                task_id: self.reviewer_memory.block_for(
                    section=changed['section'], op=changed['op'], task_id=task_id,
                    exclude_candidate_id=exclude_candidate_id)
                for task_id in panel
            }
        prediction = self.reviewer.predict(base_skill, candidate_skill, claim, panel,
                                           panel_key, memory_blocks=blocks)
        base_score = self.executor.score(base_skill, sample, panel_key)
        candidate_score = self.executor.score(candidate_skill, sample, panel_key)

        base_arm = base_score.as_arm(S.ARM_BASE, S.MODE_CONSOLIDATED_DIRECT, 'none')
        candidate_arm = candidate_score.as_arm(S.ARM_CANDIDATE,
                                               S.MODE_CONSOLIDATED_DIRECT, 'none')
        S.assert_isolation_valid([base_arm, candidate_arm], panel_key)

        base_rates = base_arm.by_task_rate()
        candidate_rates = candidate_arm.by_task_rate()
        if set(base_rates) != set(sample) or set(candidate_rates) != set(sample):
            raise InvalidInput('Sampled execution did not cover exactly the sample')
        measurements = {t: candidate_rates[t] - base_rates[t] for t in sample}
        decision = PPI.estimate(prediction.deltas, measurements,
                                confidence=self.confidence)

        # Attribution runs only where the two executions actually differ, and it
        # reads those executions back from the cache rather than rerunning them.
        divergences, verifications, changed_rule = {}, {}, None
        if self.verifier is not None:
            changed_rule = change_view(base_skill, candidate_skill)
            base_records = self.executor.records(base_skill, sample, panel_key)
            candidate_records = self.executor.records(candidate_skill, sample, panel_key)
            for task_id in sample:
                divergence = first_divergence(
                    base_records[task_id].get('events') or (),
                    candidate_records[task_id].get('events') or ())
                divergences[task_id] = divergence
                if divergence is not None:
                    verifications[task_id] = self.verifier.verify(
                        task_id, changed_rule, claim, divergence, panel_key)

        prediction_by_task = {int(row['task_id']): row for row in prediction.rows}
        rows = []
        for task_id in panel:
            predicted = prediction_by_task[task_id]
            sampled = task_id in measurements
            divergence = divergences.get(task_id) if sampled else None
            rows.append({
                'task_id': task_id,
                'delta_probability': float(predicted['delta_probability']),
                'trigger_probability': float(predicted['trigger_probability']),
                'reviewer_reason': str(predicted['reason']),
                'claim_inconsistent': bool(predicted.get('claim_inconsistent', False)),
                'sampled': sampled,
                'measured_delta': (measurements[task_id] if sampled else None),
                'from_cache': bool(predicted.get('from_cache', False)),
                'divergence': (divergence.payload() if divergence is not None else None),
                'verification': (verifications.get(task_id) if sampled else None),
            })
        return SampledValidation(
            skill_id=base_skill.skill_id,
            panel_key=panel_key,
            sample_key=sample_key,
            base_skill_key=base_skill.key,
            candidate_skill_key=candidate_skill.key,
            claim_id=claim.claim_id,
            panel_task_ids=panel,
            sample_task_ids=sample,
            decision=decision,
            arms=(base_arm, candidate_arm),
            rows=tuple(rows),
            verification_enabled=self.verifier is not None,
        )
