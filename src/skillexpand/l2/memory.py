"""The two memories the protocol gives each player, derived from one ledger.

Both players learn from the same recorded changes (:mod:`skillexpand.l2.ledger`),
but not the same thing, and not in the same form:

* **Planner** learns what its own proposals did in reality -- which kinds of
  change work, and how often its claims held.  It is built from counts and
  measurements only.  Val task text and task ids never enter it, by
  construction rather than by filtering: nothing rendered here reads a task.
  That keeps the Reviewer's advantage real -- it is the one that knows which
  tasks the change will meet -- and stops the Planner writing rules for the
  panel instead of the task.
* **Reviewer** learns where its own estimates go wrong, as concrete cases
  against val tasks: changes it expected to help that did nothing, and changes
  it dismissed that helped.  It is allowed the task text because the Reviewer
  already sees the panel, and a case without its task cannot teach anything
  about when a rule fires.

"The rule fired" always means the verifier implicated the rule, never merely
that the two trajectories diverged: an LLM executor can drift on a step the rule
does not govern.  With verification off, claim statistics are not reported.

Nothing here calls a model, so a memory can be recomputed from the journals.
"""

from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Optional, Tuple

from skillexpand.l2.ledger import EFFECT_TOLERANCE, ChangeRecord

VERDICT_EFFECTIVE = 'effective'
VERDICT_NO_EFFECT = 'no_effect'
VERDICT_INSUFFICIENT = 'insufficient'

OVER_ESTIMATED = 'over_estimated'
UNDER_ESTIMATED = 'under_estimated'
CORRECT = 'correct'

DEFAULT_RECENT = 8
DEFAULT_BLOCK_CASES = 3


def verdict(change: ChangeRecord) -> str:
    """What the sample says about one change, independent of the Reviewer."""
    measured = change.mean_measured
    if measured is None:
        return VERDICT_INSUFFICIENT
    if measured <= EFFECT_TOLERANCE:
        return VERDICT_NO_EFFECT
    return VERDICT_EFFECTIVE if change.accepted else VERDICT_INSUFFICIENT


def claim_counts(change: ChangeRecord) -> Tuple[int, int, int]:
    """``(sampled, rule implicated, claim confirmed)`` for one change."""
    sampled = change.sampled
    implicated = [row for row in sampled if row.rule_implicated]
    confirmed = sum(1 for row in implicated if row.category == 'claim_confirmed')
    return len(sampled), len(implicated), confirmed


def _number(value: Optional[float]) -> str:
    return 'n/a' if value is None else f'{value:+.2f}'


def _mean(values) -> Optional[float]:
    values = list(values)
    return sum(values) / len(values) if values else None


# --------------------------------------------------------------------------
# Planner memory: aggregates only
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PlannerMemory:
    """What the Planner's own proposals did, as counts and measurements."""

    changes: Tuple[ChangeRecord, ...]
    claims_verified: bool = True
    recent: int = DEFAULT_RECENT

    def _claims(self, sampled: int, implicated: int, confirmed: int) -> str:
        if not self.claims_verified:
            return ''
        return (f' | rule fired {implicated}/{sampled}, '
                f'matched the claim {confirmed}/{implicated}')

    def render(self) -> str:
        if not self.changes:
            return ''
        buckets: Dict[str, list] = {}
        for change in self.changes:
            buckets.setdefault(change.change_type(), []).append(change)
        lines = ['CHANGE HISTORY (what earlier proposals of yours did when the panel was '
                 'executed; counts and measurements only, no task is named or quoted)']
        for change_type, group in sorted(buckets.items()):
            verdicts = [verdict(change) for change in group]
            counts = [claim_counts(change) for change in group]
            lines.append(
                f'{change_type}: {len(group)} proposal(s) -> '
                f'{verdicts.count(VERDICT_EFFECTIVE)} effective, '
                f'{verdicts.count(VERDICT_NO_EFFECT)} with no measured effect, '
                f'{verdicts.count(VERDICT_INSUFFICIENT)} unresolved'
                f' | mean predicted '
                f'{_number(_mean(c.mean_predicted for c in group if c.sampled))}'
                f' vs mean measured '
                f'{_number(_mean(c.mean_measured for c in group if c.sampled))}'
                + self._claims(*(sum(column) for column in zip(*counts))))
        lines.append('most recent proposals:')
        for change in self.changes[-self.recent:]:
            lines.append(
                f'  round {change.round_index} {change.change_type()}: '
                f'{verdict(change)} | predicted {_number(change.mean_predicted)} vs '
                f'measured {_number(change.mean_measured)}'
                + self._claims(*claim_counts(change)))
        return '\n'.join(lines)


# --------------------------------------------------------------------------
# Reviewer memory: cases, with the panel's task text
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ReviewerCase:
    """One sampled task of one earlier change."""

    kind: str
    round_index: int
    candidate_id: str
    section: Optional[str]
    op: Optional[str]
    task_id: int
    task: str
    trigger: Optional[str]
    predicted_delta: float
    measured_delta: float
    category: Optional[str]

    def render(self) -> str:
        attribution = f'verifier: {self.category}' if self.category else 'no trajectory difference'
        trigger = f' | claimed trigger: "{self.trigger}"' if self.trigger else ''
        return (f'- {self.kind} ({attribution}): task "{self.task}"{trigger} | '
                f'reviewer predicted {_number(self.predicted_delta)}, '
                f'measured {_number(self.measured_delta)}')


def _case_kind(predicted: float, measured: float) -> str:
    if predicted > EFFECT_TOLERANCE and measured <= EFFECT_TOLERANCE:
        return OVER_ESTIMATED
    if predicted <= EFFECT_TOLERANCE and measured > EFFECT_TOLERANCE:
        return UNDER_ESTIMATED
    return CORRECT


@dataclass(frozen=True)
class ReviewerMemory:
    """Cases drawn from earlier rounds, retrieved by change type on demand."""

    cases: Tuple[ReviewerCase, ...]

    @property
    def version(self) -> int:
        """Distinct earlier changes the memory draws on; enters the cache key."""
        return len({case.candidate_id for case in self.cases})

    @classmethod
    def build(cls, changes: Iterable[ChangeRecord],
              task_text: Callable[[int], str]) -> 'ReviewerMemory':
        return cls(cases=tuple(
            ReviewerCase(
                kind=_case_kind(row.predicted_delta, float(row.measured_delta)),
                round_index=change.round_index,
                candidate_id=change.candidate_id,
                section=change.section,
                op=change.op,
                task_id=row.task_id,
                task=task_text(row.task_id),
                trigger=change.claim_trigger,
                predicted_delta=row.predicted_delta,
                measured_delta=float(row.measured_delta),
                category=row.category,
            )
            for change in changes for row in change.sampled))

    def block_for(self, *, section: Optional[str], op: Optional[str], task_id: int,
                  exclude_candidate_id: str, limit: int = DEFAULT_BLOCK_CASES) -> str:
        """Cases of the same kind of change, never the one under judgement.

        Misestimates come first, one of each kind, then the most recent rest.
        The current proposal and the current task are excluded: a case drawn
        from either would hand the Reviewer the answer it is being asked for.
        """
        pool = sorted((case for case in self.cases
                       if case.section == section and case.op == op
                       and case.task_id != task_id
                       and case.candidate_id != exclude_candidate_id),
                      key=lambda case: (-case.round_index, case.task_id))
        chosen = [next(case for case in pool if case.kind == kind)
                  for kind in (OVER_ESTIMATED, UNDER_ESTIMATED, CORRECT)
                  if any(case.kind == kind for case in pool)]
        chosen += [case for case in pool if case not in chosen]
        if not chosen:
            return ''
        return '\n'.join(
            ['REVIEWER MEMORY (earlier changes of the same kind, on other tasks of '
             'this panel; calibrate this judgement against them, never copy them)']
            + [case.render() for case in chosen[:limit]])
