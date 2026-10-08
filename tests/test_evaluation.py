import tempfile
from pathlib import Path
from typing import List

from skillexpand import schema as S
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
