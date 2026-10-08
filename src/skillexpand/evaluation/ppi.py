"""Sample-based estimate of a paired delta, with the Reviewer's prediction as
control variate (prediction-powered inference).

The Reviewer predicts a per-task delta on the whole val panel; only a random
subset of that panel is executed.  The estimate combines both:

    delta_hat = mean_panel(prediction) + mean_sample(measured - prediction)

The second term is the Reviewer's average *error* on the sampled tasks, so a
systematically biased Reviewer is corrected rather than believed.  Because the
panel mean of the predictions is a fixed number -- no sampling randomness enters
it -- the only random component is that error term.  Its paired spread is what
sets the interval width, and that spread shrinks exactly as the Reviewer becomes
accurate.  A better Reviewer therefore needs fewer executed tasks for the same
confidence, which is the whole cost argument of the protocol.

Everything here is pure, deterministic and offline: the module imports nothing
from the pipeline so the estimator can be tested on its own.
"""

import random
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from skillexpand import schema as S


#: A bound must clear zero by more than floating-point residue.  The measured
#: delta is bounded by [-1, 1] and a real improvement is orders of magnitude
#: larger, so this is a rounding guard rather than a substantive threshold.  It
#: matches the tie tolerance used by the paired comparison in ``schema``.
DECISION_TOLERANCE = 1e-9


def select_sample(task_ids: Sequence[int], size: int, key: str) -> Tuple[int, ...]:
    """Choose the executed subset deterministically from a protocol key.

    The subset must be reproducible from the journal alone, otherwise a resumed
    stage would sample differently and the audit could not replay the decision.
    ``key`` carries the batch, the candidate and the panel, so two candidates
    never share the same sample by accident and the same candidate always does.

    The result is sorted: iteration order of the caller must not leak into the
    protocol identity.
    """
    ids = tuple(sorted(int(t) for t in task_ids))
    if not ids:
        raise ValueError('cannot sample an empty panel')
    if not key:
        raise ValueError('sample selection needs a protocol key')
    if size < 1:
        raise ValueError('sample size must be positive')
    take = min(int(size), len(ids))
    seed = int(S.content_hash({'sample': key, 'panel': list(ids)}), 16)
    chosen = random.Random(seed).sample(ids, take)
    return tuple(sorted(chosen))


@dataclass(frozen=True)
class SampleDecision:
    """The estimate, its one-sided lower bound, and why the rule fired."""

    n_panel: int
    n_sample: int
    point: Optional[float]
    lower: Optional[float]
    upper: Optional[float]
    standard_error: Optional[float]
    panel_mean_prediction: Optional[float]
    sample_mean_error: Optional[float]
    confidence: float
    accepted: bool
    reason: str

    def metrics(self) -> Dict[str, Optional[float]]:
        return {
            'n_panel': float(self.n_panel),
            'n_sample': float(self.n_sample),
            'delta_estimate': self.point,
            'delta_lower': self.lower,
            'delta_upper': self.upper,
            'delta_standard_error': self.standard_error,
            'panel_mean_prediction': self.panel_mean_prediction,
            'sample_mean_error': self.sample_mean_error,
            'confidence': float(self.confidence),
        }

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.metrics(), accepted=self.accepted, reason=self.reason)


def _quantile(confidence: float, dof: int) -> float:
    """One-sided Student-t quantile; the sample is small by construction."""
    from scipy import stats

    if dof < 1:
        raise ValueError('degrees of freedom must be positive')
    return float(stats.t.ppf(confidence, dof))


def estimate(panel_predictions: Mapping[int, float],
             sample_measurements: Mapping[int, float],
             *,
             confidence: float = 0.9) -> SampleDecision:
    """Correct the panel-wide prediction with the sample's measured error.

    ``panel_predictions`` must cover the whole panel (that is what makes the
    estimator cheaper than executing it); ``sample_measurements`` holds the
    executed delta of one task each, in ``[-1, 1]``.  The sample must be a
    subset of the panel, never a superset: a measurement the Reviewer never
    predicted cannot be corrected for.
    """
    if not 0.0 < confidence < 1.0:
        raise ValueError('confidence must lie strictly between 0 and 1')
    panel = {int(t): float(v) for t, v in panel_predictions.items()}
    sample = {int(t): float(v) for t, v in sample_measurements.items()}
    if not panel:
        return SampleDecision(0, 0, None, None, None, None, None, None,
                              confidence, False, 'empty_panel')
    unknown = sorted(set(sample) - set(panel))
    if unknown:
        raise ValueError(f'sample contains unpredicted task(s): {unknown[:3]}')
    for task_id, value in sample.items():
        if not -1.0 <= value <= 1.0:
            raise ValueError(f'measured delta outside [-1, 1] for task {task_id}')

    panel_mean = sum(panel.values()) / len(panel)
    if len(sample) < 2:
        # One execution carries no measurable spread, and a decision that
        # cannot be bounded must not be taken.
        return SampleDecision(len(panel), len(sample), None, None, None, None,
                              panel_mean, None, confidence, False,
                              'insufficient_sample')

    errors = [sample[t] - panel[t] for t in sorted(sample)]
    n = len(errors)
    mean_error = sum(errors) / n
    variance = sum((e - mean_error) ** 2 for e in errors) / (n - 1)
    standard_error = (variance / n) ** 0.5
    point = panel_mean + mean_error
    half_width = _quantile(confidence, n - 1) * standard_error
    lower, upper = point - half_width, point + half_width
    return SampleDecision(
        n_panel=len(panel),
        n_sample=n,
        point=point,
        lower=lower,
        upper=upper,
        standard_error=standard_error,
        panel_mean_prediction=panel_mean,
        sample_mean_error=mean_error,
        confidence=confidence,
        accepted=lower > DECISION_TOLERANCE,
        reason=('accepted' if lower > DECISION_TOLERANCE
                else 'lower_bound_below_zero'),
    )
