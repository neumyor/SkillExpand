import pytest
import json
from types import SimpleNamespace

from skillexpand.l2 import reviewer_coevolution as RC
from skillexpand.l2.loop import EvolutionConfig, SerialEvolutionLoop
from skillexpand.runtime import reviewer_retry as RR


def outcome(task_id, success, note=""):
    return SimpleNamespace(task_id=task_id, success=success, note=note,
                           trajectory=f"trace-{task_id}-{success}")


def test_feedback_requires_exactly_paired_tasks_and_is_train_only():
    with pytest.raises(ValueError, match="same nonempty task IDs"):
        RC.make_feedback_records(
            round_index=1, batch_id="b", family_id="f",
            base_skill_key="s@v0", candidate_skill_key="s@v1",
            base_outcomes=[outcome(1, True)], candidate_outcomes=[outcome(2, False)],
        )

    rows = RC.make_feedback_records(
        round_index=1, batch_id="b", family_id="f",
        base_skill_key="s@v0", candidate_skill_key="s@v1",
        base_outcomes=[outcome(1, True), outcome(2, False)],
        candidate_outcomes=[outcome(1, False), outcome(2, True)],
        prediction_rows=[
            {"task_id": 1, "base_probability": .8, "candidate_probability": .4,
             "predicted_improve": False},
            {"task_id": 2, "base_probability": .2, "candidate_probability": .7,
             "predicted_improve": True},
        ],
    )
    assert all(row.split == "train" for row in rows)
    assert rows[0].actual_regression
    assert rows[1].actual_improve


def test_summary_and_update_are_deterministic_and_reference_feedback():
    rows = RC.make_feedback_records(
        round_index=1, batch_id="b", family_id="f",
        base_skill_key="s@v0", candidate_skill_key="s@v1",
        base_outcomes=[outcome(1, True), outcome(2, False)],
        candidate_outcomes=[outcome(1, False), outcome(2, True)],
        prediction_rows=[
            {"task_id": 1, "base_probability": .8, "candidate_probability": .4,
             "predicted_improve": False},
            {"task_id": 2, "base_probability": .2, "candidate_probability": .7,
             "predicted_improve": True},
        ],
    )
    summary = RC.summarize_feedback(rows)
    assert summary["feedback_count"] == 2
    assert summary["actual_improvements"] == 1
    assert summary["predicted_improve_precision"] == 1.0
    update = RC.build_reviewer_update(rows, generation_round=1)
    assert update.reviewer_prompt_version == 1
    assert set(update.feedback_ids) == {row.feedback_id for row in rows}
    assert "feedback_count=2" in update.calibration_block
    assert "observed feedback IDs" in update.calibration_block


def test_single_candidate_protocol_is_explicit():
    assert EvolutionConfig(candidate_count=1, single_candidate=True).single_candidate
    with pytest.raises(ValueError, match="candidate_count=1"):
        EvolutionConfig(candidate_count=3, single_candidate=True)


def test_c2_keeps_initial_prompt_while_c3_receives_rules():
    rows = RC.make_feedback_records(
        round_index=1, batch_id="b", family_id="f",
        base_skill_key="s@v0", candidate_skill_key="s@v1",
        base_outcomes=[outcome(1, True)],
        candidate_outcomes=[outcome(1, False, "answer")],
        prediction_rows=[{
            "task_id": 1, "base_probability": .2,
            "candidate_probability": .8, "predicted_improve": True,
        }],
    )
    update = RC.build_reviewer_update(rows, generation_round=1)
    loop = object.__new__(SerialEvolutionLoop)
    loop.reviewer_update = update
    loop.config = EvolutionConfig(reviewer_update_mode="summary")
    assert loop._calibration_block() == ("", 0)
    loop.config = EvolutionConfig(reviewer_update_mode="rules")
    block, version = loop._calibration_block()
    assert version == 1
    assert "rule feedback-answer" in block


def feedback(*, regression):
    return RC.make_feedback_records(
        round_index=2, batch_id="b", family_id="f", base_skill_key="s@v0",
        candidate_skill_key="s@v1", base_outcomes=[outcome(1, True)],
        candidate_outcomes=[outcome(1, not regression, "answer")],
        prediction_rows=[{"task_id": 1, "base_probability": .2,
                          "candidate_probability": .8, "predicted_improve": True}],
    )


def test_empty_observations_preserve_feedback_and_skip_model():
    rows = feedback(regression=False)
    update = RC.generate_reviewer_update(
        rows, generation_round=2, parent_version=1,
        host_factory=lambda: pytest.fail("empty evidence must not issue a request"),
    )
    assert update.rules == ()
    assert update.skip_reason == "no_observed_rules"
    assert update.generator == "program" and update.raw_output == ""
    assert update.reviewer_prompt_version == 2 and update.parent_version == 1
    assert update.feedback_ids == (rows[0].feedback_id,)
    assert update.summary == RC.summarize_feedback(rows)


def test_rule_update_recovers_many_bad_responses_with_identical_prompt(monkeypatch):
    monkeypatch.setenv("EXPE_REVIEWER_ATTEMPTS", "10")
    waits = []
    monkeypatch.setattr(RR.time, "sleep", waits.append)
    rows = feedback(regression=True)
    prompts = []

    def llm(messages, **kwargs):
        prompts.append(messages[0].content)
        return json.dumps({"rules": [{"rule_id": "r", "text": "x" * 600,
                                      "feedback_ids": [rows[0].feedback_id if len(prompts) == 9
                                                       else "invented-id"]}]})

    update = RC.generate_reviewer_update(
        rows, generation_round=2, parent_version=1,
        host_factory=lambda: SimpleNamespace(llm=llm),
    )
    assert len(prompts) == 9 and len(set(prompts)) == 1
    assert waits == [1, 2, 4, 8, 16, 30, 30, 30]
    assert update.rules[0]["feedback_ids"] == [rows[0].feedback_id]
    assert len(update.rules[0]["text"]) == 600


def test_output_error_exhaustion_does_not_swallow_programming_errors(monkeypatch):
    monkeypatch.setattr(RR.time, "sleep", lambda _: None)
    with pytest.raises(RR.ReviewerUpdateError, match="after 6 attempts"):
        RR.retry_reviewer(lambda: '{"rules":false}',
                          lambda raw: RC.parse_update_rules(raw, ["real-id"]), attempts=6)
    with pytest.raises(AttributeError):
        RR.retry_reviewer(lambda: (_ for _ in ()).throw(AttributeError("bad host")),
                          lambda raw: raw, attempts=6)


def test_feedback_ids_are_a_list_and_unknown_ids_remain_invalid():
    for ids in ("real-id", ["unknown-id"], [], [123]):
        with pytest.raises(RR.ReviewerOutputError):
            RC.parse_update_rules(json.dumps({"rules": [
                {"rule_id": "r", "text": "valid text", "feedback_ids": ids}
            ]}), ["real-id"])
