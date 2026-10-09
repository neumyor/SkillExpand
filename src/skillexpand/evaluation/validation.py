"""Paired validation and per-task caching on frozen selector-assigned Skill groups."""

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from skillexpand.runtime import agent_factory as F
from skillexpand.runtime import parallel as PL
from skillexpand import schema as S
from skillexpand.evaluation import workers as EW
from skillexpand.reliability.errors import InvalidInput, JournalConflict, StageIncomplete
from skillexpand.reliability.policies import repair_policy
from skillexpand.reliability.retry import call_with_repair, fresh
from skillexpand.reliability.units import FailureCollector, map_units

#: Why a validation run failed to produce a usable score.
REASON_NOT_MEASURED = "validation_not_measured"
REASON_WORSE = "validation_score_dropped"
REASON_TIE = "validation_score_tied"


class ScoreCache:
    """First-write-wins per-task cache keyed by execution protocol, group and Skill body."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._by_key: Dict[str, Dict[str, Any]] = {}
        self.duplicates = 0
        self._lock = threading.RLock()
        self._replay()

    @staticmethod
    def make_key(
        benchmark: str,
        panel_key: str,
        task_id: int,
        role: str,
        skill_body: Optional[str],
    ) -> str:
        """Per-task key: ``(panel, task, arm role, skill body)``.

        Per *task*, not per panel.  The score is an aggregate over the panel, but the
        cache stores individual measurements: keying on the panel as a whole would make the
        first task's result overwrite the entry for every other task's, and a resumed run
        would then report a panel it had measured one task of.  The panel key travels in
        the hash so that a task measured for one skill's panel is never served to another's.
        """
        return S.content_hash(
            {
                "b": benchmark,
                "panel": panel_key,
                "t": int(task_id),
                "role": role,
                "body": S.content_hash(skill_body or ""),
            }
        )

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._by_key.get(key)

    def put(self, key: str, record: Dict[str, Any]) -> bool:
        """Store a record unless the key is taken.  True when it was stored."""
        with self._lock:
            if key in self._by_key:
                self.duplicates += 1
                return False
            with self.path.open("a") as fh:
                fh.write(S._canonical_json(record) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._by_key[key] = record
            return True

    def __len__(self) -> int:
        return len(self._by_key)

    def _replay(self) -> None:
        if not self.path.exists():
            return
        from skillexpand.persistence.io import read_jsonl

        for record in read_jsonl(self.path):
            key = record.get("cache_key")
            if key and key not in self._by_key:
                self._by_key[key] = record


class PredictedValidationError(StageIncomplete):
    """A predicted-validation panel with retryable task-local failures.

    Successful tasks are already durable in ``ScoreCache``; a resumed stage
    requests only ``failed_task_ids``.
    """

    def __init__(self, errors: Sequence[Dict[str, Any]], expected_task_ids: Sequence[int]):
        errors = tuple(dict(error) for error in errors)
        details = ", ".join(
            f"{item['unit_id']}:{item['type']}: {item['message']}" for item in errors[:3]
        )
        omitted = len(errors) - 3
        if omitted > 0:
            details += f", ... ({omitted} more; see evaluation_errors)"
        super().__init__(
            f"Incomplete predicted validation ({len(errors)} failed task(s)): {details}", errors)
        self.errors = errors
        self.failed_task_ids = tuple(sorted(int(error["unit_id"]) for error in errors))
        self.expected_task_ids = tuple(sorted(int(task_id) for task_id in expected_task_ids))


@dataclass
class PanelScore:
    """One skill revision's measured score on one panel."""

    skill_key: Optional[str]
    body: str
    task_ids: Tuple[int, ...]
    outcomes: Tuple[S.TaskOutcome, ...]
    from_cache: int = 0
    measured: int = 0

    @property
    def n(self) -> int:
        return len(self.outcomes)

    @property
    def successes(self) -> int:
        return sum(1 for o in self.outcomes if o.success)

    @property
    def score(self) -> Optional[float]:
        """Success rate over the panel, or ``None`` when nothing was measured.

        Never 0.0 for an unmeasured panel: an unmeasured revision and a revision that
        scores zero must not compare equal, or the accept rule would promote a head
        because the candidate's measurement silently failed.
        """
        return (self.successes / self.n) if self.n else None

    def by_task_rate(self) -> Dict[int, float]:
        acc: Dict[int, List[float]] = {}
        for outcome in self.outcomes:
            acc.setdefault(outcome.task_id, []).append(1.0 if outcome.success else 0.0)
        return {task_id: sum(vals) / len(vals) for task_id, vals in acc.items()}

    def as_arm(self, arm_id: str, mode: str, fewshot_strategy: str) -> S.ArmEvaluation:
        return S.ArmEvaluation(
            arm_id=arm_id,
            role=S.ROLE_EVAL,
            mode=mode,
            skill_key=self.skill_key,
            outcomes=self.outcomes,
            executor_fresh=True,
            experience_withheld=(fewshot_strategy == "none"),
            fewshot_strategy=fewshot_strategy,
        )


def judge_validation(base: "PanelScore", candidate: "PanelScore") -> bool:
    if base.score is None or candidate.score is None:
        return False
    return candidate.score > base.score


def validation_reasons(base: PanelScore, candidate: PanelScore) -> Tuple[str, ...]:
    """Human-readable account of why a candidate was rejected."""
    if base.score is None or candidate.score is None:
        return (
            f"{REASON_NOT_MEASURED}: the panel produced no score for "
            f"{'the stable head' if base.score is None else 'the candidate'}",
        )
    if candidate.score < base.score:
        return (
            f"{REASON_WORSE}: the panel score fell from {base.score:.3f} to "
            f"{candidate.score:.3f} ({len(candidate.task_ids)} held-out "
            "task(s))",
        )
    return (
        f"{REASON_TIE}: the panel score was unchanged at {candidate.score:.3f} "
        f"over {len(candidate.task_ids)} held-out task(s); a candidate must be "
        "strictly better to replace the stable head",
    )


def library_fingerprint(skills):
    return S.content_hash(
        [
            {
                "id": s.skill_id,
                "version": s.version,
                "description": s.description,
                "body": s.body,
            }
            for s in sorted(skills, key=lambda s: s.skill_id)
        ]
    )


class FixedSkillScorer:
    """Paired execution on one Skill's immutable selector-assigned task group."""

    def __init__(self, cfg, cache, routes, test_workers=4):
        self.benchmark, self.cache, self.test_workers = cfg.benchmark.name, cache, test_workers
        from omegaconf import OmegaConf
        from skillexpand.l1.adapters import resolve
        from skillexpand.l1.adapters import PROMPT_FIELDS

        adapter = resolve(cfg)
        self.routes = routes
        self.protocol_hash = S.content_hash(
            {
                "protocol": "fixed-skill-single-attempt-v1",
                "config": OmegaConf.to_container(cfg, resolve=True),
                "tasks": F.task_table(cfg),
                "prompts": {k: getattr(adapter, k) for k in PROMPT_FIELDS},
                "routes": routes.fingerprint,
            }
        )

    def _keys(self, skill, task_ids, panel_key, role):
        """Cache keys of one panel measurement, shared by scoring and reading.

        Description does not enter execution. Equal bodies share identical
        measurements, so a description-only edit cannot win through repeated
        sampling.
        """
        identity = S.content_hash(
            {
                "protocol": self.protocol_hash,
                "panel": panel_key,
                "skill_id": skill.skill_id,
                "body": skill.body,
            }
        )
        return {
            t: ScoreCache.make_key(self.benchmark, identity, t, role, skill.body)
            for t in task_ids
        }

    def records(self, skill, task_ids, panel_key, role=S.ROLE_EVAL):
        """Cached execution records of a panel that has already been measured.

        Reading the measurements back is how a later diagnostic -- the verifier's
        attribution of a trajectory difference -- uses the same executions the
        decision rested on, instead of running anything again.
        """
        keys = self._keys(skill, tuple(task_ids), panel_key, role)
        found = {t: self.cache.get(keys[t]) for t in keys}
        missing = sorted(t for t, record in found.items() if record is None)
        if missing:
            raise InvalidInput(f"No cached execution for task(s) {missing[:3]}")
        return found

    def score(
        self,
        skill,
        task_ids,
        panel_key,
        role=S.ROLE_EVAL,
    ):
        task_ids = tuple(sorted(task_ids))
        if len(set(task_ids)) != len(task_ids) or not set(task_ids) <= set(
            self.routes.groups[skill.skill_id]
        ):
            raise JournalConflict(
                "Evaluation tasks must belong to this Skill frozen route group"
            )
        pending, records = [], {}
        keys = self._keys(skill, task_ids, panel_key, role)
        for t in task_ids:
            hit = self.cache.get(keys[t])
            if hit is not None:
                records[t] = hit
            else:
                pending.append(
                    EW.FixedSpec(
                        unit_id=keys[t],
                        benchmark=self.benchmark,
                        task_id=t,
                        skill_key=skill.key,
                        skill_body=skill.body,
                        usage_path=str(
                            self.cache.path.parent / "usage" / (keys[t] + ".json")
                        ),
                    )
                )
        cached = len(records)
        collector = FailureCollector("fixed-execution",
                                     self.cache.path.parent / "evaluation_errors")

        def sink(record):
            t = record["task_id"]
            if t not in keys or t in records:
                raise JournalConflict("Duplicate or unexpected evaluation task")
            if record.get("failure"):
                # Partial events stay with the failure as evidence, never as a score.
                collector.record(record["failure"], keys[t], evidence=record)
                return
            stored = dict(
                record,
                cache_key=keys[t],
                panel_key=panel_key,
                protocol_hash=self.protocol_hash,
                skill_id=skill.skill_id,
            )
            self.cache.put(keys[t], stored)
            records[t] = stored

        PL.run_generic(pending, EW.execute_fixed, workers=self.test_workers, on_result=sink)
        collector.raise_if_incomplete("Incomplete Skill evaluation")
        if set(records) != set(task_ids):
            raise JournalConflict("Skill evaluation returned no record for some tasks")
        outcomes = tuple(
            S.TaskOutcome(
                task_id=t,
                family_id=skill.family_id,
                role=role,
                success=bool(records[t]["success"]),
                num_steps=int(records[t].get("steps") or 0),
                truncated=bool(records[t].get("truncated")),
                note=records[t].get("failure_mode") or "solved",
            )
            for t in task_ids
        )
        return PanelScore(
            skill.key, skill.body, task_ids, outcomes, cached, len(pending)
        )

    def validate(
        self,
        skill_id: str,
        base_skill: S.Skill,
        candidate_skill: S.Skill,
        task_ids: Sequence[int],
        panel_key: str,
    ) -> S.ValidationResult:
        """Measure the candidate, read the head's cached score, and decide.

        The base arm is measured only when its score is not already cached.  That is the
        whole cost argument of the design: after the first attempt on a head, an update
        costs one candidate pass over the panel and nothing else.
        """
        if base_skill.skill_id != skill_id or candidate_skill.skill_id != skill_id:
            raise ValueError("Both arms must evaluate the same Skill")
        task_ids = [int(t) for t in task_ids]
        if not task_ids:
            raise InvalidInput("Val panel must be nonempty")
        base = self.score(base_skill, task_ids, panel_key)
        candidate = self.score(candidate_skill, task_ids, panel_key)

        base_arm = base.as_arm(S.ARM_BASE, S.MODE_CONSOLIDATED_DIRECT, "none")
        cand_arm = candidate.as_arm(
            S.ARM_CANDIDATE, S.MODE_CONSOLIDATED_DIRECT, "none"
        )
        S.assert_isolation_valid([base_arm, cand_arm], panel_key)

        paired = S.paired_delta(base_arm, cand_arm)
        passed = judge_validation(base, candidate)
        reasons: Tuple[str, ...] = () if passed else validation_reasons(base, candidate)

        return S.ValidationResult(
            skill_id=skill_id,
            panel_key=panel_key,
            task_ids=tuple(task_ids),
            base_skill_key=base_skill.key,
            candidate_skill_key=candidate_skill.key,
            arms=(base_arm, cand_arm),
            metrics={
                "mean_base": base.score,
                "mean_candidate": candidate.score,
                "success_delta": paired.mean_delta,
                "n_paired": float(paired.n_paired),
                "base_successes": float(base.successes),
                "candidate_successes": float(candidate.successes),
                "base_from_cache": float(base.from_cache),
                "candidate_from_cache": float(candidate.from_cache),
            },
            passed=passed,
            reasons=reasons,
            pairs=paired.pairs,
            returned_to_editor=False,
        )


@dataclass
class PredictedPanelScore:
    """One Skill revision's LLM-predicted score on a frozen val panel."""

    skill_key: str
    body: str
    task_ids: Tuple[int, ...]
    predictions: Tuple[Dict[str, Any], ...]
    from_cache: int = 0
    measured: int = 0

    @property
    def n(self) -> int:
        return len(self.predictions)

    @property
    def successes(self) -> int:
        return sum(1 for item in self.predictions if item["predicted_success"])

    @property
    def mean_probability(self) -> Optional[float]:
        return (sum(float(item["probability_true"]) for item in self.predictions) / self.n
                if self.n else None)

    def as_arm(self, arm_id: str) -> S.ArmEvaluation:
        outcomes = tuple(
            S.TaskOutcome(
                task_id=item["task_id"],
                family_id=self.skill_key.split("@", 1)[0].split(".", 1)[-1],
                role=S.ROLE_EVAL,
                success=bool(item["predicted_success"]),
                note=f"predicted_probability={item['probability_true']:.6f}",
            )
            for item in self.predictions
        )
        return S.ArmEvaluation(
            arm_id=arm_id, role=S.ROLE_EVAL,
            mode=S.MODE_CONSOLIDATED_DIRECT, skill_key=self.skill_key,
            outcomes=outcomes, executor_fresh=True,
            experience_withheld=True, fewshot_strategy="none",
        )


class PredictedSkillScorer:
    """Predict Skill success independently on a frozen validation panel.

    It uses the configured L2 reviewer model through the normal chat host and
    shares the route groups and paired comparison of the empirical scorer.
    """

    PROTOCOL = "predicted-val-skill-success-v2-json-schema"
    REVIEW_OUTPUT_CONTRACT = (
        "Your visible final answer MUST be exactly one JSON object with only "
        "probability_true, predicted_success, and reason. "
    )
    # The reason is audit metadata; keep it bounded without rejecting otherwise
    # valid reviewer decisions from providers that do not enforce maxLength.
    REASON_MAX_CHARS = 8192
    RESPONSE_SCHEMA = {
        "type": "object",
        "additionalProperties": False,
        "required": ["probability_true", "predicted_success", "reason"],
        "properties": {
            "probability_true": {"type": "number", "minimum": 0, "maximum": 1},
            "predicted_success": {"type": "boolean"},
            "reason": {"type": "string", "minLength": 1},
        },
    }

    @classmethod
    def response_format(cls):
        """OpenAI-compatible strict response format for the reviewer only."""
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "predicted_skill_review",
                "strict": True,
                "schema": cls.RESPONSE_SCHEMA,
            },
        }

    def __init__(self, cfg, routes, cache, workers=8, judge_factory=None,
                 threshold=0.5):
        self.cfg = cfg
        self.routes = routes
        self.cache = cache
        self.workers = max(1, int(workers))
        self.judge_factory = judge_factory
        self.threshold = float(threshold)
        # Some Tencent endpoints intermittently reject the strict wire
        # ``response_format`` (HTTP 400, code 400006) even for prompts they
        # answered identically moments earlier.  The prompt and the parser are
        # unchanged; only the wire field is omitted, and the protocol hash
        # records that so the two modes never share a cache.
        legacy_omit = os.environ.get("EXPE_LLM_REVIEWER_NO_RESPONSE_FORMAT", "").strip().lower() in {
            "1", "true", "yes", "on"
        }
        self.wire_response_format = os.environ.get(
            "EXPE_REVIEWER_RESPONSE_FORMAT", "omit" if legacy_omit else "json_schema")
        if self.wire_response_format not in {"json_schema", "omit"}:
            raise ValueError("EXPE_REVIEWER_RESPONSE_FORMAT must be json_schema or omit")
        if not 0.0 <= self.threshold <= 1.0:
            raise InvalidInput("prediction threshold must be between 0 and 1")
        self.protocol_hash = S.content_hash({
            "protocol": self.PROTOCOL,
            "response_schema": self.RESPONSE_SCHEMA,
            "benchmark": cfg.benchmark.name,
            "routes": routes.fingerprint,
            "threshold": self.threshold,
            **({"wire_response_format": "omit"} if self.wire_response_format == "omit" else {}),
        })

    def prompt(self, task: str, skill: S.Skill) -> str:
        payload = {
            "task": task,
            "skill": {"description": skill.description, "body": skill.body},
            "instructions": (
                "You are a strict validation reviewer. Predict whether a fresh "
                "executor will complete this task successfully with one autonomous "
                "attempt using this Skill. Do not assume rejected answers can be "
                "retried and do not use any execution trace. You may reason internally "
                "for as long as needed. "
                f"{self.REVIEW_OUTPUT_CONTRACT}"
                "Do not output markdown, analysis, a task/skill echo, or any other key. "
                f"probability_true is a number in [0,1]; predicted_success is true "
                f"exactly when probability_true >= {self.threshold:.6g}; reason is a "
                f"concise string of at most {self.REASON_MAX_CHARS} characters."
            ),
            "output_schema": {
                "probability_true": "number in [0,1]",
                "predicted_success": "boolean",
                "reason": f"string, <= {self.REASON_MAX_CHARS} characters",
            },
        }
        return json.dumps(payload, ensure_ascii=False)

    def _parse_response(self, raw):
        from skillexpand.runtime.json_output import extract_json
        value = extract_json(raw, required_keys=("probability_true", "predicted_success", "reason"))
        if set(value) != {"probability_true", "predicted_success", "reason"}:
            raise ValueError("Predicted reviewer must return exactly three required fields")
        probability = value["probability_true"]
        if isinstance(probability, bool) or not isinstance(probability, (int, float)):
            raise ValueError("Predicted reviewer probability_true must be numeric")
        probability = float(probability)
        if not 0.0 <= probability <= 1.0:
            raise ValueError("Predicted probability is outside [0, 1]")
        predicted_success = value["predicted_success"]
        if not isinstance(predicted_success, bool):
            raise ValueError("Predicted reviewer predicted_success must be boolean")
        elif predicted_success != (probability >= self.threshold):
            raise ValueError("predicted_success disagrees with probability_true")
        reason = value.get("reason")
        if not isinstance(reason, str):
            raise ValueError("Predicted reviewer reason must be a string")
        if not reason.strip():
            raise ValueError("Predicted reviewer reason must not be empty")
        return {
            "probability_true": probability,
            "predicted_success": predicted_success,
            "reason": reason,
            "raw": value,
            "threshold": self.threshold,
        }

    def _call(self, host, prompt):
        """Call the reviewer with its strict response schema and thinking enabled."""
        from langchain.schema import HumanMessage
        messages = [HumanMessage(content=prompt)]
        # A reasoning-only provider response cannot pass the strict JSON
        # schema, so respect the explicit global switch: recovery runs may
        # need a visible structured answer more than internal reasoning.
        request_kwargs = {
            "enable_thinking": os.environ.get("EXPE_LLM_DISABLE_THINKING", "").strip().lower()
            not in {"1", "true", "yes", "on"},
        }
        if self.wire_response_format == "json_schema":
            request_kwargs["response_format"] = self.response_format()
        return host.llm(messages, stop=[], replace_newline=False, request_kwargs=request_kwargs)

    def _review(self, host, prompt):
        result = call_with_repair(repair_policy("reviewer.predicted_val"),
                                  fresh(lambda: self._call(host, prompt)), self._parse_response)
        return result.value, result.attempts

    def score(self, skill, task_ids, panel_key):
        task_ids = tuple(sorted(int(t) for t in task_ids))
        if not task_ids or not set(task_ids) <= set(self.routes.groups[skill.skill_id]):
            raise JournalConflict("Predicted tasks must belong to the frozen Skill route group")
        keys = {
            t: ScoreCache.make_key(self.cfg.benchmark.name, panel_key, t,
                                   f"predicted:{self.protocol_hash}", skill.body)
            for t in task_ids
        }
        records, pending = {}, []
        for task_id in task_ids:
            hit = self.cache.get(keys[task_id])
            if hit is None:
                pending.append(task_id)
            else:
                records[task_id] = hit

        def one(task_id):
            if self.judge_factory is None:
                raise InvalidInput("Predicted val scorer requires a judge factory")
            host = self.judge_factory(
                task_id, skill,
                self.cache.path.parent / "usage" /
                f"predicted-{skill.skill_id}-{task_id}-{S.content_hash(skill.body)}.json",
            )
            result, format_attempts = self._review(
                host, self.prompt(F.task_text_of(self.cfg, task_id), skill))
            return {"task_id": task_id, "skill_key": skill.key,
                    "cache_key": keys[task_id], "panel_key": panel_key,
                    "protocol_hash": self.protocol_hash,
                    "format_attempts": format_attempts,
                    "response_format": self.wire_response_format, **result}

        def store(task_id, record):
            self.cache.put(keys[task_id], record)
            records[task_id] = record

        collector = FailureCollector("predicted-validation",
                                     self.cache.path.parent / "evaluation_errors")
        map_units(pending, one, workers=self.workers, collector=collector, on_success=store,
                  name=lambda task_id: keys[task_id], thread_name_prefix="predicted-val")
        if collector.failures:
            raise PredictedValidationError(collector.failures, task_ids)
        if set(records) != set(task_ids):
            raise JournalConflict("Predicted validation returned no record for some tasks")
        return PredictedPanelScore(
            skill.key, skill.body, task_ids,
            tuple(records[t] for t in task_ids),
            from_cache=len(task_ids) - len(pending), measured=len(pending),
        )

    def validate(self, skill_id: str, base_skill: S.Skill, candidate_skill: S.Skill,
                 task_ids: Sequence[int], panel_key: str) -> S.ValidationResult:
        if base_skill.skill_id != skill_id or candidate_skill.skill_id != skill_id:
            raise ValueError("Both predicted arms must evaluate the same Skill")
        base = self.score(base_skill, task_ids, panel_key)
        candidate = self.score(candidate_skill, task_ids, panel_key)
        base_arm, candidate_arm = base.as_arm(S.ARM_BASE), candidate.as_arm(S.ARM_CANDIDATE)
        paired = S.paired_delta(base_arm, candidate_arm)
        passed = (base.mean_probability is not None and candidate.mean_probability is not None
                  and candidate.mean_probability > base.mean_probability)
        return S.ValidationResult(
            skill_id=skill_id, panel_key=panel_key, task_ids=tuple(task_ids),
            base_skill_key=base.skill_key, candidate_skill_key=candidate.skill_key,
            arms=(base_arm, candidate_arm),
            metrics={
                "mean_base": base.mean_probability,
                "mean_candidate": candidate.mean_probability,
                "success_delta": (candidate.mean_probability - base.mean_probability
                                   if base.mean_probability is not None and candidate.mean_probability is not None
                                   else None),
                "predicted_base_successes": float(base.successes),
                "predicted_candidate_successes": float(candidate.successes),
                "n_paired": float(paired.n_paired),
                "base_from_cache": float(base.from_cache),
                "candidate_from_cache": float(candidate.from_cache),
            },
            passed=passed,
            reasons=() if passed else ("predicted_val_no_gain",),
            pairs=paired.pairs,
            prediction_rows=tuple(
                {
                    "task_id": int(task_id),
                    "base_probability": float(base_row["probability_true"]),
                    "candidate_probability": float(candidate_row["probability_true"]),
                    "base_predicted_success": bool(base_row["predicted_success"]),
                    "candidate_predicted_success": bool(candidate_row["predicted_success"]),
                    "predicted_improve": (
                        float(candidate_row["probability_true"])
                        > float(base_row["probability_true"])
                    ),
                    "base_reason": str(base_row.get("reason", "")),
                    "candidate_reason": str(candidate_row.get("reason", "")),
                    "reviewer_protocol_hash": str(candidate_row.get("protocol_hash", "")),
                }
                for task_id, base_row, candidate_row in zip(
                    base.task_ids, base.predictions, candidate.predictions
                )
            ),
            returned_to_editor=False,
        )
