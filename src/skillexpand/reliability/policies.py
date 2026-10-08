"""Every retry and repair budget, in one place.

``RetryPolicy`` governs transient infrastructure failures: the same request is
repeated, nothing about the experiment changes.  ``RepairPolicy`` governs model
output that violates its contract: it decides how many *model calls* a step may
make, so it is part of the experimental protocol.  Change a repair budget only
for a new run directory.

Call sites refer to repair policies by name (``repair_policy('planner.hypotheses')``);
a new call site adds one row to :data:`REPAIR`.
"""
import os
from dataclasses import dataclass, replace
from typing import Dict, Optional, Tuple

from skillexpand.reliability.errors import InvalidInput

EXHAUSTED_RETRY = 'retry'
EXHAUSTED_DEGRADE = 'degrade'


@dataclass(frozen=True)
class RetryPolicy:
    name: str
    #: ``None`` repeats until success.
    attempts: Optional[int]
    #: Seconds to wait before retry ``n`` (0-based); the last value repeats.
    delays: Tuple[float, ...]

    def delay(self, retry: int) -> float:
        return self.delays[min(retry, len(self.delays) - 1)]


@dataclass(frozen=True)
class RepairPolicy:
    name: str
    #: Fresh model calls per invocation.  Cached responses replayed on resume
    #: are re-validated but do not consume this budget.  How a retry is
    #: phrased (resample, fixed suffix, correction message) is the call site's.
    attempts: int
    #: Seconds to wait before fresh retry ``n`` (0-based); the last value repeats.
    delays: Tuple[float, ...] = (0.0,)
    #: ``retry``: raise ``RepairExhausted`` (a retryable unit failure).
    #: ``degrade``: return a degraded result the caller records as a protocol outcome.
    exhausted: str = EXHAUSTED_RETRY

    def delay(self, retry: int) -> float:
        return self.delays[min(retry, len(self.delays) - 1)]


#: Model and service endpoints are assumed to recover: congestion may make them
#: unresponsive for a while, but they are never retired mid-run.
PROVIDER = RetryPolicy('provider', None, (1, 2, 4, 8, 16, 32, 60))

#: Campaign stage restarts after a retryable failure.  ``EXPE_STAGE_ATTEMPTS``
#: bounds the attempts (0 or unset: unlimited).
CAMPAIGN_STAGE = RetryPolicy('campaign.stage', None, (15, 60, 180, 300))

#: Consecutive failed attempts of one stage, per category, before it needs
#: attention.  Exhausted repair budgets are retryable (a resumed stage
#: resamples), but a parser defect would otherwise resample forever.
STAGE_ATTEMPTS_BY_CATEGORY = {'response': 5}

_REVIEWER_BACKOFF = (1, 2, 4, 8, 16, 30)

REPAIR: Dict[str, RepairPolicy] = {p.name: p for p in (
    # Independent per-task success prediction on the frozen val panel.
    RepairPolicy('reviewer.predicted_val', 32, _REVIEWER_BACKOFF),
    # Compression of program-computed calibration statistics into rules.
    RepairPolicy('reviewer.calibration_rules', 32, _REVIEWER_BACKOFF),
    # Paired per-task delta prediction for one rule change on the val panel.
    RepairPolicy('reviewer.delta_review', 32, _REVIEWER_BACKOFF),
    # L2 Planner hypotheses and per-card Reviewer judgments.
    RepairPolicy('planner.hypotheses', 2),
    RepairPolicy('reviewer.card', 2),
    # Cold-start family discovery and initial Skill synthesis.
    RepairPolicy('discovery.tags', 3, (1, 2)),
    RepairPolicy('discovery.proposals', 3, (1, 2)),
    RepairPolicy('discovery.assignment', 3, (1, 2)),
    RepairPolicy('discovery.initial_skill', 3, (1, 2)),
    # Campaign preflight probe of the card Reviewer's output format.
    RepairPolicy('campaign.reviewer_probe', 3),
    # Protocol outcomes: an invalid response is recorded, not retried.
    RepairPolicy('editor.candidate', 1, exhausted=EXHAUSTED_DEGRADE),
    RepairPolicy('patterns.batch', 1, exhausted=EXHAUSTED_DEGRADE),
    RepairPolicy('selector.route', 1, exhausted=EXHAUSTED_DEGRADE),
)}

#: Environment overrides of repair budgets, for experiments that need a different one.
ATTEMPT_OVERRIDES = {
    'reviewer.predicted_val': 'EXPE_REVIEWER_ATTEMPTS',
    'reviewer.calibration_rules': 'EXPE_REVIEWER_ATTEMPTS',
    'reviewer.delta_review': 'EXPE_REVIEWER_ATTEMPTS',
}


def repair_policy(name: str) -> RepairPolicy:
    policy = REPAIR[name]
    variable = ATTEMPT_OVERRIDES.get(name)
    if variable and os.environ.get(variable):
        attempts = int(os.environ[variable])
        if attempts < 1:
            raise InvalidInput(f'{variable} must be positive')
        policy = replace(policy, attempts=attempts)
    return policy


def stage_policy() -> RetryPolicy:
    attempts = int(os.environ.get('EXPE_STAGE_ATTEMPTS', '0'))
    if attempts < 0:
        raise InvalidInput('Stage attempt budget must be nonnegative (0 means unlimited)')
    return replace(CAMPAIGN_STAGE, attempts=attempts or None)
