"""Paired validation and per-task caching on frozen selector-assigned Skill groups."""

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from skillexpand.runtime import agent_factory as F
from skillexpand.runtime import parallel as PL
from skillexpand import schema as S
from skillexpand.persistence.artifacts import provider_signature

#: Why a validation run failed to produce a usable score.
REASON_NOT_MEASURED = "validation_not_measured"
REASON_NO_GAIN = "no_validation_gain"
REASON_WORSE = "validation_score_dropped"
REASON_TIE = "validation_score_tied"
REASON_EMPTY_PANEL = "empty_validation_panel"


def canonical_patch_body(body: str) -> str:
    """Normalize incidental whitespace for exact candidate duplicate detection."""
    lines = [line.strip() for line in (body or "").splitlines()]
    return "\n".join(line for line in lines if line)


def patch_hash(body: str) -> str:
    """Content hash of a patch's resulting body, for the duplicate guard."""
    return S.content_hash(canonical_patch_body(body))


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
        from skillexpand.persistence.store import read_jsonl

        for record in read_jsonl(self.path):
            key = record.get("cache_key")
            if key and key not in self._by_key:
                self._by_key[key] = record


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
            experience_withheld=(fewshot_strategy == F.FEWSHOT_NONE),
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

    def __init__(self, cfg, cache, routes, workers=4):
        self.benchmark, self.cache, self.workers = cfg.benchmark.name, cache, workers
        from omegaconf import OmegaConf
        from skillexpand.l1.adapters import resolve
        from skillexpand.l1.adapters import PROMPT_FIELDS

        adapter = resolve(cfg)
        self.routes = routes
        self.protocol_hash = S.content_hash(
            {
                "protocol": "fixed-skill-single-attempt-v1",
                "provider": provider_signature(),
                "config": OmegaConf.to_container(cfg, resolve=True),
                "tasks": F.task_table(cfg),
                "prompts": {k: getattr(adapter, k) for k in PROMPT_FIELDS},
                "routes": routes.fingerprint,
            }
        )

    def score(
        self,
        skill,
        task_ids,
        panel_key,
        role=S.ROLE_EVAL,
        mode=S.MODE_CONSOLIDATED_DIRECT,
    ):
        task_ids = tuple(sorted(task_ids))
        if len(set(task_ids)) != len(task_ids) or not set(task_ids) <= set(
            self.routes.groups[skill.skill_id]
        ):
            raise ValueError(
                "Evaluation tasks must belong to this Skill frozen route group"
            )
        # Description does not enter execution. Equal bodies share identical measurements,
        # so a description-only edit cannot win through repeated sampling.
        identity = S.content_hash(
            {
                "protocol": self.protocol_hash,
                "panel": panel_key,
                "skill_id": skill.skill_id,
                "body": skill.body,
            }
        )
        pending, records = [], {}
        keys = {
            t: ScoreCache.make_key(self.benchmark, identity, t, role, skill.body)
            for t in task_ids
        }
        for t in task_ids:
            hit = self.cache.get(keys[t])
            if hit is not None:
                records[t] = hit
            else:
                pending.append(
                    PL.UnitSpec(
                        unit_id=keys[t],
                        benchmark=self.benchmark,
                        task_id=t,
                        role=role,
                        arm_id=S.ARM_EVAL,
                        mode=mode,
                        fewshot_strategy="none",
                        skill_key=skill.key,
                        skill_body=skill.body,
                        usage_path=str(
                            self.cache.path.parent / "usage" / (keys[t] + ".json")
                        ),
                    )
                )
        cached = len(records)
        errors = []

        def sink(record):
            from skillexpand.l1.runner import save

            t = record["task_id"]
            if t not in keys or t in records:
                raise ValueError("Duplicate or unexpected evaluation task")
            if record.get("error"):
                save(
                    self.cache.path.parent / "evaluation_errors" / (keys[t] + ".json"),
                    record,
                )
                errors.append(record)
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

        PL.run_generic(pending, PL.execute_fixed, workers=self.workers, on_result=sink)
        if errors or set(records) != set(task_ids):
            raise RuntimeError(
                "Incomplete Skill evaluation; resume retries failed units"
            )
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
            raise ValueError("Admission panel must be nonempty")
        base = self.score(base_skill, task_ids, panel_key)
        candidate = self.score(candidate_skill, task_ids, panel_key)

        base_arm = base.as_arm(S.ARM_BASE, S.MODE_CONSOLIDATED_DIRECT, F.FEWSHOT_NONE)
        cand_arm = candidate.as_arm(
            S.ARM_CANDIDATE, S.MODE_CONSOLIDATED_DIRECT, F.FEWSHOT_NONE
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
