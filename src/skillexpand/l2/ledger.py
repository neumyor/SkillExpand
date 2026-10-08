"""The ledger of the sampled protocol: one row per proposed change and task.

The ledger is not a file of its own.  It is a read-only view over the batch
journals, which already hold the claim, the Reviewer's per-task prediction, the
sampled measurement and the verifier's verdict on the two executions.
Deriving it rather than writing it keeps a single source of truth: both
memories and every reported Reviewer metric can be recomputed at any time from
what was journaled, and none of them can drift from the decisions they describe.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

#: A measured or predicted delta is "positive" above this.  It matches the
#: decision's rounding guard, so "no effect" and "not accepted" agree.
EFFECT_TOLERANCE = 1e-9

#: Verifier verdicts under which the changed rule changed the execution.  A
#: difference alone does not mean the rule fired: an LLM executor can drift on a
#: step the rule does not govern, which the verifier labels ``unrelated``.
RULE_IMPLICATED = ('claim_confirmed', 'claim_not_confirmed')


@dataclass(frozen=True)
class ChangeRow:
    """One val task of one proposed change."""

    task_id: int
    predicted_delta: float
    measured_delta: Optional[float]
    sampled: bool
    category: Optional[str]

    @property
    def rule_implicated(self) -> bool:
        return self.category in RULE_IMPLICATED


@dataclass(frozen=True)
class ChangeRecord:
    """One proposed change and everything the journals recorded about it."""

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
        return _mean([row.predicted_delta for row in self.sampled])

    @property
    def mean_measured(self) -> Optional[float]:
        return _mean([float(row.measured_delta) for row in self.sampled])

    def change_type(self) -> str:
        return f'{self.section or "unknown"}/{self.op or "unknown"}'


def _mean(values: Sequence[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _candidate_edit(journal: Dict[str, Any], candidate_id: str):
    for row in journal.get('proposals', ()):
        raw = row.get('edit', {}).get('candidate')
        if raw and raw.get('candidate_id') == candidate_id and raw.get('edits'):
            edit = raw['edits'][0]
            op = 'add' if str(edit.get('op', '')).upper() == 'ADD' else 'replace'
            return edit.get('section'), op
    return None, None


def _claim_trigger(journal: Dict[str, Any], candidate_id: str) -> Optional[str]:
    for row in journal.get('proposals', ()):
        raw = row.get('edit', {}).get('candidate')
        if raw and raw.get('candidate_id') == candidate_id:
            return (row.get('claim') or {}).get('trigger')
    return None


def changes_from_journal(journal: Dict[str, Any]) -> Tuple[ChangeRecord, ...]:
    """The ledger rows of one batch journal; empty unless it used sampled acceptance."""
    acceptance = journal.get('acceptance') or {}
    if acceptance.get('mode') != 'sampled':
        return ()
    records = []
    for entry in acceptance.get('candidates', ()):
        result = entry.get('result') or {}
        decision = result.get('decision') or {}
        candidate_id = str(entry.get('candidate_id', ''))
        section, op = _candidate_edit(journal, candidate_id)
        rows = tuple(
            ChangeRow(
                task_id=int(row['task_id']),
                predicted_delta=float(row['delta_probability']),
                measured_delta=(None if row.get('measured_delta') is None
                                else float(row['measured_delta'])),
                sampled=bool(row.get('sampled')),
                category=(row.get('verification') or {}).get('category'),
            )
            for row in result.get('rows', ()))
        records.append(ChangeRecord(
            round_index=int(journal.get('round', 0)),
            batch_id=str(journal.get('batch_id', '')),
            family_id=str(journal.get('family_id', '')),
            candidate_id=candidate_id,
            section=section,
            op=op,
            claim_trigger=_claim_trigger(journal, candidate_id),
            accepted=bool(decision.get('accepted')),
            point=decision.get('point'),
            lower=decision.get('lower'),
            rows=rows,
        ))
    return tuple(records)


def read_changes(root, *, before_round: Optional[int] = None) -> Tuple[ChangeRecord, ...]:
    """Every sampled change the journals recorded, oldest round first.

    ``before_round`` keeps a round out of its own present: a round may learn
    only from rounds that finished before it.
    """
    directory = Path(root) / 'l2_batches'
    if not directory.exists():
        return ()
    records = []
    for path in sorted(directory.glob('*.json')):
        journal = json.loads(path.read_text())
        if before_round is not None and int(journal.get('round', 0)) >= before_round:
            continue
        records.extend(changes_from_journal(journal))
    return tuple(sorted(records, key=lambda item: (item.round_index, item.batch_id,
                                                   item.candidate_id)))


def reviewer_metrics(changes: Sequence[ChangeRecord]) -> Dict[str, Any]:
    """The pre-registered Reviewer metrics over every sampled pair.

    The primary metric is the per-task delta Brier score against the baseline
    that predicts no effect anywhere (skill score > 0 means the Reviewer beats
    "nothing changes").  Candidate-level false acceptance is the guardrail: a
    Reviewer that only becomes more conservative lowers it without becoming more
    accurate.  Only point values are reported: no confidence interval is
    computed for these metrics.
    """
    pairs = [(row.predicted_delta, float(row.measured_delta))
             for change in changes for row in change.sampled]
    brier = _mean([(p - m) ** 2 for p, m in pairs])
    baseline = _mean([m ** 2 for _, m in pairs])
    accepted = [change for change in changes if change.accepted]
    return {
        'sampled_pairs': len(pairs),
        'candidates': len(changes),
        'delta_brier': brier,
        'delta_brier_zero_baseline': baseline,
        'delta_brier_skill': (None if not baseline else 1.0 - brier / baseline),
        'accepted': len(accepted),
        'false_accepts': sum(1 for change in accepted
                             if (change.mean_measured or 0.0) <= EFFECT_TOLERANCE),
    }
