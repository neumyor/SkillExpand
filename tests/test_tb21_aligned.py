import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("tb21_aligned", SCRIPTS / "run_tb21_aligned.py")
aligned = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aligned)


def raw(tmp_path, model, attempt, *, reward=1, exception=None, steps=2):
    directory = tmp_path / f"{model}-{attempt}"
    aligned.write(directory / "result.json", {"task_name": "task", "exception_info": exception,
        "config": {"agent": {"model_name": "openai/" + model}},
        "verifier_result": {"rewards": {"reward": reward}}})
    aligned.write(directory / "agent/trajectory.json", {"steps": [{"message": f"observation {i}"} for i in range(steps)]})


def test_raw_timeout_preserves_unknown_reward_and_unique_source_keys(tmp_path):
    for a in range(1, 4):
        raw(tmp_path, "deepseek", a, reward=None,
            exception={"exception_type": "AgentTimeoutError"})
    rows = aligned.source_rows(tmp_path, [{"task_name": "task"}], "deepseek")
    assert len(rows) == 3 and all(r["reward"] is None for r in rows)
    assert len({(r["source_model"], r["task_id"], r["attempt_index"]) for r in rows}) == 3
    raw(tmp_path, "deepseek", 4)
    with pytest.raises(ValueError, match="coverage"):
        aligned.source_rows(tmp_path, [{"task_name": "task"}], "deepseek")


def test_mixed_evidence_is_balanced_without_source_substitution(tmp_path):
    rows = []
    for model, steps in [("qwen", 4), ("deepseek", 1)]:
        source = tmp_path / model
        for a in range(1, 4):
            raw(source, model, a, steps=steps)
        rows += aligned.source_rows(source, [{"task_name": "task"}], model)
    payload = aligned.source_payloads(0, "instruction", rows)
    assert [len(p["trials"]) for p in payload] == [3, 3]
    assert [len(p["evidence"]) for p in payload] == [3, 3]
    with pytest.raises(ValueError, match="Missing source"):
        aligned.source_payloads(0, "instruction", rows[:-1])


def test_model_specific_verifier_validation(tmp_path):
    raw(tmp_path, aligned.METHOD, 1)
    path = tmp_path / f"{aligned.METHOD}-1/result.json"
    assert aligned.validate_trial(path, "task", model=aligned.METHOD)[1] == 1
    with pytest.raises(ValueError, match="executor_model"):
        aligned.validate_trial(path, "task")


def test_free_generation_rejects_empty_body_and_task_assignments():
    import propose_terminalbench_library as propose
    row = {"skill_id": "terminalbench.family-p001", "family_id": "family-p001", "name": "generated",
           "description": "routing", "trigger_conditions": ["compile"], "body": "inspect and compile"}
    assert len(propose.validate_proposal(json.dumps({"skills": [row]}))) == 1
    with pytest.raises(RuntimeError, match="membership"):
        propose.validate_proposal(json.dumps({"skills": [{**row, "task_ids": [0]}]}))
    with pytest.raises(RuntimeError, match="fields"):
        propose.validate_proposal(json.dumps({"skills": [{**row, "body": ""}]}))


def test_reviewer_budget_survives_resume_without_reset(tmp_path):
    class Scorer:
        def _call(self, host, prompt):
            return "malformed"

        def _parse(self, raw):
            raise ValueError("invalid")

    scorer = Scorer()
    class Loop:
        def _ensure_predicted_scorer(self):
            return scorer
    prompt = json.dumps({"task": "task", "skill": {"body": "rules"}})
    aligned.install_review_budget(Loop(), tmp_path, [{"instruction": "task"}])
    with pytest.raises(ValueError, match="budget exhausted"):
        scorer._review(None, prompt)
    calls = []
    scorer._call = lambda *args: calls.append(args)
    aligned.install_review_budget(Loop(), tmp_path, [{"instruction": "task"}])
    with pytest.raises(ValueError, match="budget exhausted"):
        scorer._review(None, prompt)
    assert calls == []


def test_existing_family_scorer_rejects_other_family_tasks():
    from skillexpand.evaluation.validation import PredictedSkillScorer
    from skillexpand import schema as S
    skill = S.Skill("terminalbench.family-p001", "family-p001", 0, "generated", "routing", "rules")
    scorer = PredictedSkillScorer(SimpleNamespace(benchmark=SimpleNamespace(name="terminalbench")),
        SimpleNamespace(fingerprint="new-library", groups={skill.skill_id: (0, 2)}), None)
    with pytest.raises(ValueError, match="route group"):
        scorer.score(skill, (0, 1, 2), "closed-set-family")


@pytest.mark.parametrize("count", [1, 3])
def test_materializer_accepts_freely_generated_library(tmp_path, monkeypatch, count):
    import materialize_terminalbench_library as materialize
    from skillexpand.runtime.progressive import catalog
    from skillexpand import schema as S
    trial = {"index": 1, "phase": "autonomous", "status": "completed", "success": True,
             "termination": "verifier", "trajectory": "path", "events": []}
    exp = aligned.experience(0, "task", [trial])
    aligned.write(tmp_path / "discovery/results/0.json", S.to_dict(exp))
    proposal = tmp_path / "proposal.json"
    aligned.write(proposal, {"skills": [{"skill_id": f"terminalbench.family-p{i:03d}",
        "family_id": f"family-p{i:03d}", "name": "generated", "description": "routing", "body": "generated rules"}
        for i in range(1, count + 1)]})
    monkeypatch.setattr(sys, "argv", ["materialize", "--run-dir", str(tmp_path), "--proposal", str(proposal)])
    assert materialize.main() == 0
    assert aligned.read(tmp_path / "cold_start_complete.json")["model_generated_library"]
    skills = [S.from_dict(S.Skill, s) for s in aligned.read(tmp_path / "initial_skills.json")]
    assert len(skills) == count and all("body" not in item for item in catalog(skills))
