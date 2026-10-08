"""The two memories the protocol gives each player, derived from one ledger.

Both players learn from the same recorded batches, but not the same thing, and
not in the same form:

* **Planner** learns what its own proposals did in reality -- which kinds of
  change work, and how often its claims held.  It is built from counts and
  measurements only.  Val task text and task ids never enter it, by
  construction rather than by filtering: the record type has no field that could
  carry them.  That is what keeps the Reviewer's advantage real -- it is the one
  that knows which tasks the change will meet -- and it stops the Planner from
  writing rules for the panel instead of the task.
* **Reviewer** learns where its own estimates go wrong, as concrete cases
  against val tasks: changes it expected to help that did nothing, and changes
  it dismissed that helped.  It is allowed the task text because the Reviewer
  already sees the panel, and a case without its task cannot teach anything
  about when a rule fires.

Nothing here calls a model.  A memory is a view over the batch journals, so it
can be recomputed at any time from what was already written down.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Sequence, Tuple


#: A measured gain is judged against this, in the delta's own units.  Matches the
#: decision's rounding guard so that "no effect" and "not accepted" agree.
EFFECT_TOLERANCE = 1e-9

VERDICT_EFFECTIVE = 'effective'
VERDICT_NO_EFFECT = 'no_effect'
VERDICT_INSUFFICIENT = 'insufficient'

#: How a Reviewer case relates its prediction to the measurement.
OVER_ESTIMATED = 'over_estimated'
UNDER_ESTIMATED = 'under_estimated'
CORRECT = 'correct'

#: Retained per change type, so its counts cover the whole history rather than
#: only the lines that happen to be shown.
DEFAULT_RECENT = 8
DEFAULT_BLOCK_CASES = 3


def _tolerance():
    return EFFECT_TOLERANCE


@dataclass(frozen=True)
class ChangeRow:
    """One sampled task of one proposed change."""

    task_id: int
    predicted_delta: float
    measured_delta: Optional[float]
    sampled: bool
    diverged: bool
    category: Optional[str]


@dataclass(frozen=True)
class ChangeRecord:
    """One proposed change and everything the ledger recorded about it."""

    round_index: int
    batch_id: str
    family_id: str
    candidate_id: str
    section: Optional[str]
    op: Optional[str]
    claim_trigger: Optional[str]
    accepted: bool
    point: Optional[float]
    lower: Optional[float]
    rows: Tuple[ChangeRow, ...]

    @property
    def sampled(self) -> Tuple[ChangeRow, ...]:
        return tuple(row for row in self.rows if row.sampled)

    @property
    def mean_predicted(self) -> Optional[float]:
        sampled = self.sampled
        if not sampled:
            return None
        return sum(row.predicted_delta for row in sampled) / len(sampled)

    @property
    def mean_measured(self) -> Optional[float]:
        sampled = self.sampled
        if not sampled:
            return None
        return sum(float(row.measured_delta) for row in sampled) / len(sampled)

    @property
    def verdict(self) -> str:
        measured = self.mean_measured
        if measured is None:
            return VERDICT_INSUFFICIENT
        if measured <= _tolerance():
            return VERDICT_NO_EFFECT
        return VERDICT_EFFECTIVE if self.accepted else VERDICT_INSUFFICIENT

    @property
    def claim_fired(self) -> Tuple[int, int]:
        sampled = self.sampled
        return sum(1 for row in sampled if row.diverged), len(sampled)

    @property
    def claim_matched(self) -> Tuple[int, int]:
        sampled = self.sampled
        fired = [row for row in sampled if row.diverged]
        return (sum(1 for row in fired if row.category == 'claim_confirmed'),
                len(fired))

    def change_type(self) -> str:
        return f'{self.section or "unknown"}/{self.op or "unknown"}'


def _candidate_edit(journal: Dict[str, Any], candidate_id: str):
    for row in journal.get('proposals', ()):
        raw = row.get('edit', {}).get('candidate')
        if raw and raw.get('candidate_id') == candidate_id:
            edits = raw.get('edits') or ()
            if edits:
                edit = edits[0]
                op = 'add' if str(edit.get('op', '')).upper() == 'ADD' else 'replace'
                return edit.get('section'), op
    return None, None


def _claim_trigger(journal: Dict[str, Any], index: int) -> Optional[str]:
    hypotheses = journal.get('hypotheses') or ()
    if 0 <= index < len(hypotheses):
        claim = hypotheses[index].get('claim') or {}
        return claim.get('trigger')
    return None


def read_changes(root, *, before_round: Optional[int] = None,
                 modes: Sequence[str] = ('sampled',)) -> Tuple[ChangeRecord, ...]:
    """Every proposed change the ledger recorded, oldest round first.

    ``before_round`` is how the Reviewer's memory stays out of its own present:
    a round may only learn from rounds that finished before it.
    """
    directory = Path(root) / 'l2_batches'
    if not directory.exists():
        return ()
    records = []
    for path in sorted(directory.glob('*.json')):
        journal = json.loads(path.read_text())
        round_index = int(journal.get('round', 0))
        if before_round is not None and round_index >= before_round:
            continue
        acceptance = journal.get('acceptance') or {}
        if acceptance.get('mode') not in modes:
            continue
        for index, entry in enumerate(acceptance.get('candidates', ())):
            result = entry.get('result') or {}
            candidate_id = str(entry.get('candidate_id', ''))
            section, op = _candidate_edit(journal, candidate_id)
            decision = result.get('decision') or {}
            rows = []
            for row in result.get('rows', ()):
                rows.append(ChangeRow(
                    task_id=int(row['task_id']),
                    predicted_delta=float(row['delta_probability']),
                    measured_delta=(None if row.get('measured_delta') is None
                                    else float(row['measured_delta'])),
                    sampled=bool(row.get('sampled')),
                    diverged=row.get('divergence') is not None,
                    category=(row.get('verification') or {}).get('category'),
                ))
            records.append(ChangeRecord(
                round_index=round_index,
                batch_id=str(journal.get('batch_id', '')),
                family_id=str(journal.get('family_id', '')),
                candidate_id=candidate_id,
                section=section,
                op=op,
                claim_trigger=_claim_trigger(journal, index),
                accepted=bool(decision.get('accepted')),
                point=(None if decision.get('point') is None else float(decision['point'])),
                lower=(None if decision.get('lower') is None else float(decision['lower'])),
                rows=tuple(rows),
            ))
    return tuple(sorted(records, key=lambda item: (item.round_index, item.batch_id,
                                                   item.candidate_id)))


# --------------------------------------------------------------------------
# Planner memory: aggregates only
# --------------------------------------------------------------------------

def _number(value: Optional[float], places: int = 2) -> str:
    return 'n/a' if value is None else f'{value:+.{places}f}'


@dataclass(frozen=True)
class PlannerMemory:
    """What the Planner's own proposals did, as counts and measurements.

    Every field is a number or a fixed vocabulary word; there is deliberately no
    field a val task's text or id could travel in.
    """

    changes: Tuple[ChangeRecord, ...]
    recent: int = DEFAULT_RECENT

    @property
    def empty(self) -> bool:
        return not self.changes

    def by_type(self) -> Dict[str, Dict[str, Any]]:
        summary: Dict[str, Dict[str, Any]] = {}
        for change in self.changes:
            bucket = summary.setdefault(change.change_type(), {
                'count': 0, VERDICT_EFFECTIVE: 0, VERDICT_NO_EFFECT: 0,
                VERDICT_INSUFFICIENT: 0, 'predicted': [], 'measured': [],
                'fired': 0, 'checked': 0, 'matched': 0, 'diverged': 0})
            bucket['count'] += 1
            bucket[change.verdict] += 1
            predicted, measured = change.mean_predicted, change.mean_measured
            if predicted is not None:
                bucket['predicted'].append(predicted)
            if measured is not None:
                bucket['measured'].append(measured)
            fired, checked = change.claim_fired
            matched, diverged = change.claim_matched
            bucket['fired'] += fired
            bucket['checked'] += checked
            bucket['matched'] += matched
            bucket['diverged'] += diverged
        return summary

    def render(self) -> str:
        if self.empty:
            return ''
        lines = [
            'CHANGE HISTORY (what earlier proposals of yours did when the panel was '
            'executed; counts and measurements only, no task is named or quoted)',
        ]
        for change_type, bucket in sorted(self.by_type().items()):
            predicted = bucket['predicted']
            measured = bucket['measured']
            mean_predicted = sum(predicted) / len(predicted) if predicted else None
            mean_measured = sum(measured) / len(measured) if measured else None
            lines.append(
                f'{change_type}: {bucket["count"]} proposal(s) -> '
                f'{bucket[VERDICT_EFFECTIVE]} effective, '
                f'{bucket[VERDICT_NO_EFFECT]} with no measured effect, '
                f'{bucket[VERDICT_INSUFFICIENT]} unresolved'
                f' | mean predicted {_number(mean_predicted)} vs mean measured '
                f'{_number(mean_measured)}'
                f' | rule fired {bucket["fired"]}/{bucket["checked"]}, matched the '
                f'claim {bucket["matched"]}/{bucket["diverged"]}')
        lines.append('most recent proposals:')
        for change in self.changes[-self.recent:]:
            fired, checked = change.claim_fired
            matched, diverged = change.claim_matched
            lines.append(
                f'  round {change.round_index} {change.change_type()}: '
                f'{change.verdict} | predicted {_number(change.mean_predicted)} vs '
                f'measured {_number(change.mean_measured)} | rule fired '
                f'{fired}/{checked}, matched the claim {matched}/{diverged}')
        lines.append(
            'Use this to stop repeating a kind of change that measured nothing, and '
            'to state a claim precisely enough that it can be checked. It says '
            'nothing about which tasks the change will meet.')
        return '\n'.join(lines)


# --------------------------------------------------------------------------
# Reviewer memory: cases, with the panel's task text
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ReviewerCase:
    """One sampled task of one earlier change, where the estimate missed."""

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
    diverged: bool
    category: Optional[str]

    def render(self) -> str:
        situation = 'the rule fired' if self.diverged else 'the rule never fired'
        attribution = f', verifier: {self.category}' if self.category else ''
        trigger = f' | claimed trigger: "{self.trigger}"' if self.trigger else ''
        return (f'{self.kind} ({situation}{attribution}): task "{self.task}"'
                f'{trigger} | reviewer predicted {_number(self.predicted_delta)}, '
                f'measured {_number(self.measured_delta)}')


def _case_kind(predicted: float, measured: float) -> str:
    if predicted > _tolerance() and measured <= _tolerance():
        return OVER_ESTIMATED
    if predicted <= _tolerance() and measured > _tolerance():
        return UNDER_ESTIMATED
    return CORRECT


@dataclass(frozen=True)
class ReviewerMemory:
    """Cases drawn from earlier rounds, retrieved by change type on demand."""

    cases: Tuple[ReviewerCase, ...]
    version: int = 0

    @classmethod
    def build(cls, changes: Iterable[ChangeRecord],
              task_text: Callable[[int], str]) -> 'ReviewerMemory':
        cases = []
        for change in changes:
            for row in change.sampled:
                if row.measured_delta is None:
                    continue
                cases.append(ReviewerCase(
                    kind=_case_kind(row.predicted_delta, row.measured_delta),
                    round_index=change.round_index,
                    candidate_id=change.candidate_id,
                    section=change.section,
                    op=change.op,
                    task_id=row.task_id,
                    task=task_text(row.task_id),
                    trigger=change.claim_trigger,
                    predicted_delta=row.predicted_delta,
                    measured_delta=float(row.measured_delta),
                    diverged=row.diverged,
                    category=row.category,
                ))
        return cls(cases=tuple(cases),
                   version=len({case.candidate_id for case in cases}))

    def block_for(self, *, section: Optional[str], op: Optional[str], task_id: int,
                  exclude_candidate_id: str, limit: int = DEFAULT_BLOCK_CASES) -> str:
        """Cases of the same kind of change, never the one under judgement.

        The current proposal and the current task are excluded: a case drawn from
        either would hand the Reviewer the answer it is being asked for.
        """
        pool = [case for case in self.cases
                if case.section == section and case.op == op
                and case.task_id != task_id
                and case.candidate_id != exclude_candidate_id]
        if not pool:
            return ''
        pool.sort(key=lambda case: (-case.round_index, case.task_id))
        chosen, seen_kinds = [], set()
        for wanted in (OVER_ESTIMATED, UNDER_ESTIMATED, CORRECT):
            for case in pool:
                if case.kind == wanted:
                    chosen.append(case)
                    seen_kinds.add(wanted)
                    break
        for case in pool:
            if len(chosen) >= limit:
                break
            if case not in chosen:
                chosen.append(case)
        lines = [
            'REVIEWER MEMORY (earlier changes of the same kind, on other tasks of '
            'this panel; calibrate this judgement against them, never copy them)',
        ]
        lines.extend(case.render() for case in chosen[:limit])
        if len(chosen) > limit:
            lines.append(f'({len(chosen) - limit} further case(s) omitted)')
        return '\n'.join(lines)
