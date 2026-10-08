"""Auditable data contracts for Reviewer co-evolution.

The main L2 loop deliberately knows how to propose and accept Skills, but it
should not also own the statistical definition of Reviewer calibration.  This
module keeps that definition small, deterministic, and usable by both the live
loop and offline audit tools.
"""

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from skillexpand import schema as S
from skillexpand.reliability.errors import SchemaViolation
from skillexpand.reliability.policies import repair_policy
from skillexpand.reliability.retry import call_with_repair, fresh


PROTOCOL = "reviewer-coevolution-feedback-v1"
MAX_RULES = 4


@dataclass(frozen=True)
class FixedTrainRoutes:
    """A deterministic family-to-train-task panel for feedback execution.

    Reviewer feedback must not call the selector or borrow the val route.  The
    audited train family map is the route, and its fingerprint is part of the
    scorer cache identity.
    """

    groups: Dict[str, Tuple[int, ...]]
    fingerprint: str

    @classmethod
    def from_plan(cls, plan):
        groups = {
            f"{plan.benchmark}.{family}": tuple(sorted(int(task) for task in tasks))
            for family, tasks in sorted(plan.families.items())
        }
        return cls(groups=groups, fingerprint=S.content_hash({"protocol": PROTOCOL, "groups": groups}))


def _success(value: Any) -> bool:
    if isinstance(value, Mapping):
        return bool(value.get("success"))
    return bool(getattr(value, "success"))


def _task_id(value: Any) -> int:
    if isinstance(value, Mapping):
        return int(value["task_id"])
    return int(getattr(value, "task_id"))


def _note(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("note", ""))
    return str(getattr(value, "note", ""))


def _trace_hash(value: Any) -> str:
    if isinstance(value, Mapping):
        trajectory = value.get("trajectory", "")
    else:
        trajectory = getattr(value, "trajectory", "")
    return S.content_hash(str(trajectory or ""))


def _error_category(success: bool, note: str) -> str:
    if success:
        return "success"
    lowered = note.casefold()
    for key in ("timeout", "route", "service", "parse", "step", "answer"):
        if key in lowered:
            return key
    return "failure"


def _prediction_probability(row: Mapping[str, Any]) -> float:
    base = float(row.get("base_probability", 0.5))
    candidate = float(row.get("candidate_probability", 0.5))
    # A compact relative ranking value; absolute calibration metrics use the
    # base/candidate probabilities directly.
    return max(0.0, min(1.0, 0.5 + candidate - base))


@dataclass(frozen=True)
class PairedFeedback:
    """One old/candidate outcome pair on one fixed train task."""

    feedback_id: str
    protocol: str
    round_index: int
    batch_id: str
    task_id: int
    family_id: str
    base_skill_key: str
    candidate_skill_key: str
    base_success: bool
    candidate_success: bool
    predicted_improve: Optional[bool]
    predicted_probability: Optional[float]
    base_probability: Optional[float]
    candidate_probability: Optional[float]
    base_error_category: str
    candidate_error_category: str
    base_trace_hash: str
    candidate_trace_hash: str
    split: str = S.SPLIT_TRAIN
    route_fingerprint: str = ""
    panel_key: str = ""
    executor_protocol: str = ""
    reviewer_prompt_version: int = 0
    reviewer_protocol_hash: str = ""

    @property
    def actual_improve(self) -> bool:
        return self.candidate_success and not self.base_success

    @property
    def actual_regression(self) -> bool:
        return self.base_success and not self.candidate_success

    def __post_init__(self):
        if self.protocol != PROTOCOL:
            raise ValueError(f"unknown feedback protocol {self.protocol!r}")
        if self.split != S.SPLIT_TRAIN:
            raise ValueError("Reviewer feedback must be train-only")
        if self.round_index < 1 or self.task_id < 0:
            raise ValueError("feedback round and task id must be nonnegative/positive")
        if self.predicted_probability is not None and not 0.0 <= self.predicted_probability <= 1.0:
            raise ValueError("predicted probability must be in [0, 1]")
        if self.reviewer_prompt_version < 0:
            raise ValueError("reviewer prompt version must be nonnegative")


@dataclass(frozen=True)
class ReviewerUpdate:
    """Versioned calibration memory consumed by the next Reviewer round."""

    reviewer_prompt_version: int
    parent_version: Optional[int]
    generation_round: int
    feedback_ids: Tuple[str, ...]
    summary: Dict[str, Any]
    rules: Tuple[Dict[str, Any], ...] = ()
    scope: str = "train_feedback_only"
    failure_conditions: Tuple[str, ...] = (
        "does not apply outside the observed feedback subset",
        "does not change historical predictions or output schema",
    )
    calibration_block: str = ""
    protocol: str = PROTOCOL
    generator: str = "program"
    input_prompt: str = ""
    raw_output: str = ""
    skip_reason: str = ""

    def __post_init__(self):
        if self.protocol != PROTOCOL:
            raise ValueError(f"unknown Reviewer update protocol {self.protocol!r}")
        if self.reviewer_prompt_version < 1:
            raise ValueError("Reviewer update versions start at 1")
        if self.parent_version is not None and self.parent_version >= self.reviewer_prompt_version:
            raise ValueError("Reviewer update parent must precede child version")
        if self.generation_round < 1:
            raise ValueError("Reviewer update generation round must be positive")
        if not self.feedback_ids:
            raise ValueError("Reviewer update requires at least one feedback id")
        if len(self.rules) > MAX_RULES:
            raise ValueError(f"Reviewer update may contain at most {MAX_RULES} rules")
        object.__setattr__(self, "feedback_ids", tuple(self.feedback_ids))
        object.__setattr__(self, "rules", tuple(dict(rule) for rule in self.rules))
        object.__setattr__(self, "failure_conditions", tuple(self.failure_conditions))
        if self.generator not in ("program", "llm"):
            raise ValueError("unknown Reviewer update generator")


def make_feedback_records(
    *,
    round_index: int,
    batch_id: str,
    family_id: str,
    base_skill_key: str,
    candidate_skill_key: str,
    base_outcomes: Sequence[Any],
    candidate_outcomes: Sequence[Any],
    prediction_rows: Sequence[Mapping[str, Any]] = (),
    route_fingerprint: str = "",
    panel_key: str = "",
    executor_protocol: str = "",
    reviewer_prompt_version: int = 0,
    reviewer_protocol_hash: str = "",
) -> Tuple[PairedFeedback, ...]:
    """Pair exactly one old and candidate outcome per task.

    The function rejects missing, duplicate, or mismatched task IDs.  That check
    is the important part of the protocol: an aggregate success rate cannot
    establish a paired improvement when the two arms did not see the same tasks.
    """
    base = {_task_id(item): item for item in base_outcomes}
    candidate = {_task_id(item): item for item in candidate_outcomes}
    if len(base) != len(base_outcomes) or len(candidate) != len(candidate_outcomes):
        raise ValueError("paired feedback requires unique task IDs in each arm")
    if not base or set(base) != set(candidate):
        raise ValueError("paired feedback arms must cover the same nonempty task IDs")
    predictions = {int(row["task_id"]): row for row in prediction_rows}
    if len(predictions) != len(prediction_rows) or not set(predictions) <= set(base):
        raise ValueError("prediction rows must have unique task IDs within the feedback panel")
    records = []
    for task_id in sorted(base):
        old, new = base[task_id], candidate[task_id]
        row = predictions.get(task_id)
        if row is not None:
            base_probability = float(row["base_probability"])
            candidate_probability = float(row["candidate_probability"])
            if not (0.0 <= base_probability <= 1.0 and
                    0.0 <= candidate_probability <= 1.0):
                raise ValueError("Reviewer probabilities must be in [0, 1]")
            if bool(row["predicted_improve"]) != (candidate_probability > base_probability):
                raise ValueError("predicted_improve disagrees with Reviewer probabilities")
        predicted_probability = _prediction_probability(row) if row else None
        payload = {
            "protocol": PROTOCOL,
            "round": int(round_index),
            "batch": batch_id,
            "task": task_id,
            "family": family_id,
            "base": base_skill_key,
            "candidate": candidate_skill_key,
            "base_success": _success(old),
            "candidate_success": _success(new),
            "predicted_probability": predicted_probability,
            "route_fingerprint": route_fingerprint,
            "panel_key": panel_key,
            "executor_protocol": executor_protocol,
            "reviewer_prompt_version": reviewer_prompt_version,
            "reviewer_protocol_hash": reviewer_protocol_hash,
        }
        records.append(
            PairedFeedback(
                feedback_id=S.content_hash(payload),
                protocol=PROTOCOL,
                round_index=int(round_index),
                batch_id=str(batch_id),
                task_id=task_id,
                family_id=str(family_id),
                base_skill_key=str(base_skill_key),
                candidate_skill_key=str(candidate_skill_key),
                base_success=_success(old),
                candidate_success=_success(new),
                predicted_improve=(bool(row["predicted_improve"]) if row else None),
                predicted_probability=predicted_probability,
                base_probability=(float(row["base_probability"]) if row else None),
                candidate_probability=(float(row["candidate_probability"]) if row else None),
                base_error_category=_error_category(_success(old), _note(old)),
                candidate_error_category=_error_category(_success(new), _note(new)),
                base_trace_hash=_trace_hash(old),
                candidate_trace_hash=_trace_hash(new),
                split=S.SPLIT_TRAIN,
                route_fingerprint=str(route_fingerprint),
                panel_key=str(panel_key),
                executor_protocol=str(executor_protocol),
                reviewer_prompt_version=int(reviewer_prompt_version),
                reviewer_protocol_hash=str(reviewer_protocol_hash),
            )
        )
    return tuple(records)


def _ece_absolute(rows: Sequence[PairedFeedback], arm: str, bins: int = 10) -> Optional[float]:
    usable = [r for r in rows if getattr(r, f"{arm}_probability") is not None]
    if not usable:
        return None
    total = 0.0
    for index in range(bins):
        lo = index / bins
        hi = (index + 1) / bins
        bucket = [r for r in usable if lo <= getattr(r, f"{arm}_probability") < hi or
                  (index == bins - 1 and getattr(r, f"{arm}_probability") <= hi)]
        if not bucket:
            continue
        mean_prob = sum(getattr(r, f"{arm}_probability") for r in bucket) / len(bucket)
        mean_actual = sum(getattr(r, f"{arm}_success") for r in bucket) / len(bucket)
        total += len(bucket) / len(usable) * abs(mean_prob - mean_actual)
    return total


def summarize_feedback(records: Sequence[PairedFeedback]) -> Dict[str, Any]:
    """Compute the registered calibration summary from raw paired records."""
    rows = tuple(records)
    if not rows:
        raise ValueError("cannot summarize empty feedback")
    predicted = [r for r in rows if r.predicted_improve is not None]
    predicted_improvements = [r for r in predicted if r.predicted_improve]
    true_improvements = [r for r in rows if r.actual_improve]
    false_positive = [r for r in predicted_improvements if r.actual_regression]
    false_negative = [r for r in predicted if not r.predicted_improve and r.actual_improve]
    absolute_rows = [r for r in rows
                     if r.base_probability is not None and r.candidate_probability is not None]
    base_brier = (
        sum((r.base_probability - float(r.base_success)) ** 2 for r in absolute_rows)
        / len(absolute_rows) if absolute_rows else None
    )
    candidate_brier = (
        sum((r.candidate_probability - float(r.candidate_success)) ** 2 for r in absolute_rows)
        / len(absolute_rows) if absolute_rows else None
    )
    brier = ((base_brier + candidate_brier) / 2
             if base_brier is not None and candidate_brier is not None else None)
    base_ece = _ece_absolute(rows, "base")
    candidate_ece = _ece_absolute(rows, "candidate")
    ece = ((base_ece + candidate_ece) / 2
           if base_ece is not None and candidate_ece is not None else None)
    return {
        "feedback_count": len(rows),
        "predicted_count": len(predicted),
        "base_successes": sum(r.base_success for r in rows),
        "candidate_successes": sum(r.candidate_success for r in rows),
        "actual_improvements": len(true_improvements),
        "predicted_improvements": len(predicted_improvements),
        "predicted_improve_precision": (
            sum(r.actual_improve for r in predicted_improvements) / len(predicted_improvements)
            if predicted_improvements else None
        ),
        "false_positive_regression_rate": (
            len(false_positive) / len(predicted_improvements)
            if predicted_improvements else None
        ),
        "false_negative_rate": (
            len(false_negative) / len(true_improvements)
            if true_improvements else None
        ),
        "paired_accuracy": (
            sum(bool(r.predicted_improve) == r.actual_improve for r in predicted) / len(predicted)
            if predicted else None
        ),
        "brier_score": brier,
        "base_brier_score": base_brier,
        "candidate_brier_score": candidate_brier,
        "ece": ece,
        "base_ece": base_ece,
        "candidate_ece": candidate_ece,
        "error_categories": {
            category: sum(r.candidate_error_category == category for r in rows)
            for category in sorted({r.candidate_error_category for r in rows})
        },
    }


def _rules_from_feedback(records: Sequence[PairedFeedback], summary: Mapping[str, Any]) -> Tuple[Dict[str, Any], ...]:
    rules = []
    by_category = {}
    for row in records:
        if row.predicted_improve is True and row.actual_regression:
            by_category.setdefault(row.candidate_error_category, []).append(row.feedback_id)
    for category, ids in sorted(by_category.items(), key=lambda item: (-len(item[1]), item[0])):
        rules.append({
            "rule_id": f"feedback-{category}",
            "text": f"Treat candidate changes associated with observed {category} outcomes cautiously.",
            "feedback_ids": ids,
            "observed_count": len(ids),
        })
        if len(rules) >= MAX_RULES:
            break
    return tuple(rules)


def render_calibration_block(summary: Mapping[str, Any], rules: Sequence[Mapping[str, Any]]) -> str:
    """Render only program-computed facts into the next Reviewer prompt."""
    lines = [
        "CALIBRATION FEEDBACK (observed train paired outcomes only):",
        f"feedback_count={int(summary['feedback_count'])}",
        f"predicted_improvements={int(summary['predicted_improvements'])}",
        f"actual_improvements={int(summary['actual_improvements'])}",
        f"false_positive_regression_rate={summary['false_positive_regression_rate']}",
    ]
    for rule in rules:
        ids = ",".join(str(value) for value in rule["feedback_ids"])
        lines.append(f"rule {rule['rule_id']}: {rule['text']} [feedback_ids={ids}]")
    lines.append("Do not generalize beyond these observed feedback IDs.")
    return "\n".join(lines)


def build_reviewer_update(
    records: Sequence[PairedFeedback],
    *,
    generation_round: int,
    parent_version: Optional[int] = None,
    rules: Optional[Sequence[Mapping[str, Any]]] = None,
    generator: str = "program",
    input_prompt: str = "",
    raw_output: str = "",
    skip_reason: str = "",
) -> ReviewerUpdate:
    rows = tuple(records)
    summary = summarize_feedback(rows)
    rules = tuple(dict(rule) for rule in (rules if rules is not None else _rules_from_feedback(rows, summary)))
    version = 1 if parent_version is None else parent_version + 1
    return ReviewerUpdate(
        reviewer_prompt_version=version,
        parent_version=parent_version,
        generation_round=generation_round,
        feedback_ids=tuple(row.feedback_id for row in rows),
        summary=summary,
        rules=rules,
        calibration_block=render_calibration_block(summary, rules),
        generator=generator,
        input_prompt=input_prompt,
        raw_output=raw_output,
        skip_reason=skip_reason,
    )


def update_prompt(summary: Mapping[str, Any], observed_rules: Sequence[Mapping[str, Any]]) -> str:
    """Ask the Reviewer to compress only program-computed observations."""
    payload = {
        "summary": dict(summary),
        "observed_rules": [dict(rule) for rule in observed_rules],
        "instructions": (
            "Return JSON only with a rules list of at most 4 objects. Each object must "
            "contain rule_id, text, and feedback_ids. Use only the supplied feedback IDs "
            "and facts. Do not invent task outcomes, causes, or retry policies."
        ),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def parse_update_rules(raw: str, valid_feedback_ids: Sequence[str]) -> Tuple[Dict[str, Any], ...]:
    """Validate the LLM's compression output against the factual feedback set."""
    try:
        return _parse_update_rules(raw, valid_feedback_ids)
    except (ValueError, KeyError, TypeError) as exc:
        raise SchemaViolation(str(exc)) from exc


def _parse_update_rules(raw: str, valid_feedback_ids: Sequence[str]) -> Tuple[Dict[str, Any], ...]:
    from skillexpand.runtime.json_output import extract_json

    value = extract_json(raw, required_keys=("rules",))
    if set(value) != {"rules"} or not isinstance(value["rules"], list):
        raise ValueError("Reviewer update must contain only a rules list")
    if len(value["rules"]) > MAX_RULES:
        raise ValueError(f"Reviewer update may contain at most {MAX_RULES} rules")
    valid = set(valid_feedback_ids)
    result = []
    for rule in value["rules"]:
        if not isinstance(rule, Mapping) or set(rule) != {"rule_id", "text", "feedback_ids"}:
            raise ValueError("Reviewer rule schema mismatch")
        if not isinstance(rule["feedback_ids"], list) or not all(
                isinstance(item, str) for item in rule["feedback_ids"]):
            raise ValueError("Reviewer rule feedback_ids must be a list of strings")
        ids = tuple(rule["feedback_ids"])
        if not ids or not set(ids) <= valid:
            raise ValueError("Reviewer rule references unknown feedback")
        text = rule["text"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Reviewer rule text is invalid")
        result.append({"rule_id": str(rule["rule_id"]), "text": text.strip(),
                       "feedback_ids": list(ids), "observed_count": len(ids)})
    return tuple(result)


def generate_reviewer_update(records, *, generation_round, parent_version=None,
                             host_factory=None):
    rows = tuple(records)
    summary = summarize_feedback(rows)
    observed = _rules_from_feedback(rows, summary)
    prompt = update_prompt(summary, observed)
    if not observed:
        return build_reviewer_update(
            rows, generation_round=generation_round, parent_version=parent_version,
            rules=(), input_prompt=prompt, skip_reason="no_observed_rules",
        )
    from langchain.schema import HumanMessage

    host = host_factory()
    raw_outputs = []

    def call():
        raw = host.llm([HumanMessage(content=prompt)], stop=[], replace_newline=False)
        raw_outputs.append(raw)
        return raw

    rules = call_with_repair(
        repair_policy("reviewer.calibration_rules"), fresh(call),
        lambda raw: parse_update_rules(raw, [row.feedback_id for row in rows]),
    ).value
    return build_reviewer_update(
        rows, generation_round=generation_round, parent_version=parent_version,
        rules=rules, generator="llm", input_prompt=prompt,
        raw_output=str(raw_outputs[-1]),
    )


def validate_feedback_records(records: Iterable[Mapping[str, Any]], *, split: str = S.SPLIT_TRAIN,
                              strict: bool = False) -> None:
    """Audit the leakage-sensitive part of the feedback artifact."""
    if split != S.SPLIT_TRAIN:
        raise ValueError("Reviewer feedback must be generated from train only")
    ids = set()
    for row in records:
        feedback_id = row.get("feedback_id")
        if not feedback_id or feedback_id in ids:
            raise ValueError("feedback IDs must be unique")
        ids.add(feedback_id)
        if row.get("protocol") != PROTOCOL:
            raise ValueError("feedback protocol mismatch")
        if row.get("split", S.SPLIT_TRAIN) != S.SPLIT_TRAIN:
            raise ValueError("feedback artifact contains a non-train row")
        if not row.get("base_skill_key") or not row.get("candidate_skill_key"):
            raise ValueError("feedback is missing Skill keys")
        if strict:
            for key in ("route_fingerprint", "panel_key", "executor_protocol",
                        "reviewer_protocol_hash"):
                if not row.get(key):
                    raise ValueError(f"feedback is missing {key}")
            if int(row.get("reviewer_prompt_version", -1)) < 0:
                raise ValueError("feedback has invalid reviewer prompt version")
