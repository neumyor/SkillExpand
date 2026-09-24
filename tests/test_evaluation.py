import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from skillexpand import schema as S
from skillexpand.evaluation import selector as SE
from skillexpand.evaluation import splits as SP
from skillexpand.persistence import store as ST
from skillexpand.evaluation import validation as VA

FAILURES: List[str] = []


def check(name: str, fn) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        FAILURES.append(f"{name}: {type(exc).__name__}: {exc}")
        print(f"  FAIL  {name}\n        {type(exc).__name__}: {exc}")
    else:
        print(f"  ok    {name}")


def expect_raises(exc_type, fn, *a, **kw) -> None:
    try:
        fn(*a, **kw)
    except exc_type:
        return
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            f"expected {exc_type.__name__}, got {type(exc).__name__}: {exc}"
        ) from exc
    raise AssertionError(f"expected {exc_type.__name__}, nothing raised")


def eq(a, b, what: str = "") -> None:
    if a != b:
        raise AssertionError(f"{what}: {a!r} != {b!r}")


def truthy(value, what: str = "") -> None:
    if not value:
        raise AssertionError(f"{what}: expected truthy, got {value!r}")


def falsy(value, what: str = "") -> None:
    if value:
        raise AssertionError(f"{what}: expected falsy, got {value!r}")


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

BENCH = "alfworld"
FAMILIES = ["pick_heat_then_place", "pick_cool_then_place"]
SKILL_IDS = [f"{BENCH}.{f}" for f in FAMILIES]


def make_attempt(
    patch_id: str,
    skill_id: str,
    consumed: Sequence[str],
    outcome: str = S.UPDATE_REJECTED,
    head_version: int = 0,
    meta_version: int = 0,
    trigger: str = S.TRIGGER_POOL_THRESHOLD,
    before: float = 0.5,
    after: float = 0.375,
    body: str = "1. a rule",
    created_at: str = "2026-01-01T00:00:00+00:00",
    attempt_index: int = 0,
    pool_cleared: bool = False,
) -> S.PatchAttempt:
    """A patch attempt whose verdict and scores are controlled by the arguments."""
    head_key = f"{skill_id}@v{head_version}"
    task_ids = (1, 2)
    arms = tuple(
        S.ArmEvaluation(
            arm_id=arm,
            role=S.ROLE_EVAL,
            mode=S.MODE_CONSOLIDATED_DIRECT,
            skill_key=head_key,
            outcomes=tuple(
                S.TaskOutcome(
                    task_id=t,
                    family_id="f",
                    role=S.ROLE_EVAL,
                    success=(t <= (before * len(task_ids))),
                )
                for t in task_ids
            ),
            executor_fresh=True,
            experience_withheld=True,
            fewshot_strategy="none",
        )
        for arm in (S.ARM_BASE, S.ARM_CANDIDATE)
    )
    validation = S.ValidationResult(
        skill_id=skill_id,
        panel_key=f"{skill_id}#panel2",
        task_ids=task_ids,
        base_skill_key=head_key,
        candidate_skill_key=f"{skill_id}@v{head_version + 1}",
        arms=arms,
        metrics={
            "mean_base": before,
            "mean_candidate": after,
            "success_delta": after - before,
            "n_paired": float(len(task_ids)),
        },
        passed=(outcome == S.UPDATE_ACCEPTED),
        pairs=tuple((t, before, after) for t in task_ids),
        returned_to_editor=False,
    )
    record = S.PatchAttemptRecord(
        patch_id=f"{patch_id}-record",
        stable_head_key=head_key,
        patch_hash=VA.patch_hash(body),
        candidate_body=body,
        validation_before=before,
        validation_after=after,
        verdict=(
            S.VERDICT_ACCEPT if outcome == S.UPDATE_ACCEPTED else S.VERDICT_REJECT
        ),
        created_at=created_at,
        reasons=() if outcome == S.UPDATE_ACCEPTED else ("tie",),
    )
    return S.PatchAttempt(
        patch_id=patch_id,
        benchmark=BENCH,
        skill_id=skill_id,
        skill_family_id=skill_id.partition(".")[2],
        stable_head_key=head_key,
        candidate_skill_key=f"{skill_id}@v{head_version + 1}",
        meta_skill_version=meta_version,
        pooled_experience_ids=tuple(consumed),
        pooled_task_ids=(1,),
        validation_panel_key=f"{skill_id}#panel2",
        validation_task_ids=task_ids,
        validation=(None if outcome == S.UPDATE_DUPLICATE_PATCH else validation),
        patch=record,
        outcome=outcome,
        trigger=trigger,
        attempt_index=attempt_index,
        pool_cleared=pool_cleared,
        created_at=created_at,
    )




# --------------------------------------------------------------------------
# The trigger is a count
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Rejection retains the pool and requires new evidence
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# The reject buffer
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Validation primitives
# --------------------------------------------------------------------------


def test_validation_reasons_name_the_failure() -> None:
    class Score:
        def __init__(self, value, ids=(1, 2)):
            self.score = value
            self.task_ids = ids

    reasons = VA.validation_reasons(Score(0.5), Score(0.5))
    truthy(any("tied" in r for r in reasons), f"a tie is named as one: {reasons}")
    reasons = VA.validation_reasons(Score(0.5), Score(0.25))
    truthy(any("fell" in r for r in reasons), f"a drop is named: {reasons}")
    reasons = VA.validation_reasons(Score(None), Score(0.25))
    truthy(
        any(VA.REASON_NOT_MEASURED in r for r in reasons),
        f"an unmeasured panel is named: {reasons}",
    )


def test_panel_score_reports_none_when_nothing_was_measured() -> None:
    empty = VA.PanelScore(skill_key="s@v0", body="", task_ids=(1, 2, 3), outcomes=())
    eq(empty.score, None, "an unmeasured panel is not a zero score")
    eq(empty.n, 0, "and has no outcomes")


def test_panel_score_is_the_success_rate_and_reports_a_cached_mix() -> None:
    outcomes = (
        S.TaskOutcome(
            task_id=1,
            family_id="f",
            role=S.ROLE_EVAL,
            success=True,
            note="solved;cached",
        ),
        S.TaskOutcome(
            task_id=2, family_id="f", role=S.ROLE_EVAL, success=False, note="max_steps"
        ),
    )
    score = VA.PanelScore(
        skill_key="s@v0",
        body="1. a",
        task_ids=(1, 2),
        outcomes=outcomes,
        from_cache=1,
        measured=1,
    )
    eq(score.score, 0.5, "one of two")
    eq(score.by_task_rate(), {1: 1.0, 2: 0.0}, "per-task rates")
    arm = score.as_arm(S.ARM_BASE, S.MODE_CONSOLIDATED_DIRECT, "none")
    eq(arm.role, S.ROLE_EVAL, "the arm carries the evaluation role")
    truthy(arm.experience_withheld, "and declares the isolation it was run under")
    S.assert_isolation_valid([arm], "test")


def test_score_cache_key_is_per_task_and_first_write_wins(tmp=None) -> None:
    tmp = Path(tempfile.mkdtemp())
    cache = VA.ScoreCache(tmp / "scores.jsonl")
    k1 = VA.ScoreCache.make_key(BENCH, "panelA", 1, S.ROLE_EVAL, "1. rule A")
    k2 = VA.ScoreCache.make_key(BENCH, "panelA", 1, S.ROLE_EVAL, "1. rule A")
    k3 = VA.ScoreCache.make_key(BENCH, "panelA", 2, S.ROLE_EVAL, "1. rule A")
    k4 = VA.ScoreCache.make_key(BENCH, "panelA", 1, S.ROLE_EVAL, "1. rule B")
    k5 = VA.ScoreCache.make_key(BENCH, "panelB", 1, S.ROLE_EVAL, "1. rule A")
    eq(k1, k2, "same panel, task and body -> same key")
    truthy(k1 != k3, "a different task is a different key")
    truthy(k1 != k4, "a different body is a different key")
    truthy(k1 != k5, "and another skill panel is a different key")

    truthy(cache.put(k1, {"cache_key": k1, "success": True}), "first write stored")
    falsy(cache.put(k1, {"cache_key": k1, "success": False}), "second write refused")
    eq(cache.duplicates, 1, "and counted")
    eq(cache.get(k1)["success"], True, "the first measurement survives")

    reloaded = VA.ScoreCache(tmp / "scores.jsonl")
    eq(len(reloaded), 1, "and it replays from disk")


def test_score_cache_tolerates_a_truncated_final_line() -> None:
    tmp = Path(tempfile.mkdtemp())
    path = tmp / "scores.jsonl"
    path.write_text('{"cache_key": "abc", "score": 0.5}\n{"cache_key": "def", "sco')
    cache = VA.ScoreCache(path)
    eq(len(cache), 1, "the partial final line is dropped, not fatal")


# --------------------------------------------------------------------------
# The meta pool
# --------------------------------------------------------------------------






def test_acceptance_rate_excludes_unmeasured_attempts() -> None:
    skill_id = SKILL_IDS[0]
    accepted = make_attempt("p1", skill_id, (), outcome=S.UPDATE_ACCEPTED)
    duplicate = make_attempt("p2", skill_id, (), outcome=S.UPDATE_DUPLICATE_PATCH)
    eq(
        S.patch_acceptance_rate([accepted, duplicate]),
        1.0,
        "the duplicate was never measured, so it cannot lower the rate",
    )
    eq(
        S.patch_acceptance_rate([duplicate]),
        None,
        "and a batch with nothing measured has no rate at all",
    )


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


def test_validation_requires_strict_improvement():
    for base, candidate, expected in (
        (0.5, 0.75, True),
        (0.5, 0.5, False),
        (0.5, 0.25, False),
        (None, 1.0, False),
    ):
        eq(
            VA.judge_validation(
                type("Score", (), {"score": base})(),
                type("Score", (), {"score": candidate})(),
            ),
            expected,
        )
