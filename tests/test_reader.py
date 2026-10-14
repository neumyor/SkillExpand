import json
from pathlib import Path

from skillexpand.reader.data import (FileCache, Reader, evidence_step, matching_steps,
                                    outcome, skill_diff, trajectory_html)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def skill(version, body):
    return {"skill_id": "s", "version": version, "name": "skill", "body": body,
            "description": "scope", "provenance": {"source_experience_ids": ["q:0"]}}


def trial(root, task, attempt, request, reward=None, exception=None):
    req = root / "slots" / f"{task:02}-{attempt}" / "requests" / f"{request:03}"
    p = req / "jobs" / "job" / "trial" / "agent" / "trajectory.json"
    write(p, {"schema_version": "ATIF-v1.6", "agent": {"model_name": "model"},
              "steps": [{"step_id": 8, "source": "agent", "message": "inspect",
                         "reasoning_content": "recorded thought", "tool_calls": [{"arguments": {"keystrokes": "ls"}}],
                         "observation": {"results": [{"content": "<script>alert(1)</script>"}]},
                         "metrics": {"completion_tokens": 2}}]})
    write(p.parent.parent / "result.json", {"verifier_result": {"rewards": {"reward": reward}}, "exception_info": exception})
    write(req / "selection.json", {"loaded_skill_key": "s@v0", "why": "route"})
    mounted = req / "skills" / "task" / "SKILL.md"
    mounted.parent.mkdir(parents=True, exist_ok=True)
    mounted.write_text("actually mounted v0")
    return p


def experiment(tmp_path, name="first"):
    r = tmp_path / name
    write(r / "manifest.json", {"executor_model": "model"})
    write(r / "tasks.json", [{"task_name": "shared-name", "instruction": "task"}])
    return r


def test_atif_preserves_tools_reasoning_observation_metrics_and_escapes_html():
    steps = [{"step_id": 3, "source": "agent", "message": "Hello", "reasoning_content": "Think",
              "tool_calls": [{"function_name": "bash", "arguments": {"keystrokes": "ls"}}],
              "observation": {"results": [{"content": "<script>bad()</script>"}]}, "metrics": {"tokens": 7}}]
    rendered = trajectory_html(steps)
    assert all(x in rendered for x in ["Think", "bash", "keystrokes", "tokens", "&lt;script&gt;"])
    assert "<script>bad" not in rendered
    assert matching_steps(steps, "LS") == [0]
    assert matching_steps(steps, "absent") == []
    assert evidence_step({"step_id": 3}, steps) == 0
    assert evidence_step({"id": "t1:e3", "observation": "unknown"}, steps) is None
    assert evidence_step({"observation": "Hello"}, steps) == 0
    assert evidence_step({"observation": "Hello"}, steps * 2) is None


def test_cache_reloads_changes_and_tolerates_incomplete_files(tmp_path):
    p = tmp_path / "file.json"
    cache = FileCache()
    write(p, {"value": 1})
    assert cache.read(p)[0] == {"value": 1}
    write(p, {"value": 200})
    assert cache.read(p)[0] == {"value": 200}
    p.write_text('{"value":')
    assert "暂不可读" in cache.read(p)[1]
    write(p, {"value": 3})
    assert cache.read(p)[0] == {"value": 3}
    p.unlink()
    assert "缺失文件" in cache.read(p)[1]


def test_retries_adoption_real_reward_and_missing_reward(tmp_path):
    r = experiment(tmp_path)
    one = trial(r, 0, 1, 1)
    two = trial(r, 0, 1, 2, 0, {"exception_type": "AgentTimeoutError"})
    write(r / "slots/00-1/record.json", {"task_id": 0, "attempt_index": 1, "task_name": "shared-name",
          "status": "valid", "reward": 0, "trajectory_path": str(two), "result_path": str(two.parent.parent / "result.json")})
    reader = Reader(tmp_path)
    traces = reader.traces(str(r), "0")
    assert len(traces) == 2
    assert [t.key for t in traces if t.accepted] == [str(two)]
    first = reader.trace_detail(next(t for t in traces if t.key == str(one)))
    second = reader.trace_detail(next(t for t in traces if t.key == str(two)))
    assert first["reward"] is None and first["outcome"] == "未解决"
    assert second["reward"] == 0 and second["exception"]["exception_type"] == "AgentTimeoutError"
    assert second["outcome"] == "失败"
    assert next(t for t in traces if t.key == str(two)).skill_file.read_text() == "actually mounted v0"
    assert outcome(None) == "未解决"
    assert len(reader.task_choices(str(r), status="失败")) == 1


def test_source_cards_remain_independent_and_revision_is_distinct(tmp_path):
    name = "tb21-qwen-deepseek-library-qwen-selector-20261011"
    old = tmp_path / name
    new = old / "revisions/178-cards-v1"
    write(old / "manifest.json", {})
    write(new / "manifest.json", {})
    cards = [{"source_card_id": f"{model}:{i}", "task_id": i, "source_model": model,
              "experience_card": {"card_id": f"{model}:{i}", "task": {"task_id": i}, "evidence": []}}
             for model in ["qwen", "deepseek"] for i in range(89)]
    write(new / "source_cards.json", cards)
    reader = Reader(tmp_path)
    assert str(old) in reader.runs and str(new) in reader.runs
    assert len(reader.runs[str(new)].cards) == 178
    assert len({c.identity for c in reader.runs[str(new)].cards}) == 178
    assert not reader.runs[str(old)].cards
    assert len(reader.task_cards(str(new), "0")) == 2


def test_versions_frozen_source_l2_links_and_same_task_matching(tmp_path):
    r = experiment(tmp_path)
    other = experiment(tmp_path, "final")
    write(other / "manifest.json", {"source_run": str(r)})
    write(other / "tasks.json", [{"task_id": 42, "task_name": "shared-name"}])
    write(r / "initial_skills.json", [skill(0, "old\n")])
    (r / "skills.jsonl").write_text("\n".join(json.dumps(skill(v, body)) for v, body in [(0, "old\n"), (1, "new\n")]))
    write(other / "library.json", [skill(0, "frozen old\n")])
    write(r / "discovery/results/0.json", {"experience_id": "q:0", "task_id": 0,
          "experience_card": {"card_id": "q:0", "task": {"task_id": 0}, "evidence": []}})
    journal = {"batch_id": "b", "round": 1, "skill_id": "s", "experience_ids": ["q:0"],
               "task_ids": [0], "acceptance_mode": "predicted", "empirically_validated": False}
    write(r / "evolution/round-1/batches.json", [journal])
    write(r / "l2_batches/b.json", journal)
    reader = Reader(tmp_path)
    rows = reader.skills(str(other))
    assert next(s for s in rows if s["kind"] == "冻结库")["skill"]["body"] == "frozen old\n"
    assert any(s["key"] == "s@v1" for s in rows)
    assert reader.same_task(str(other), "shared-name") == "42"
    assert reader.same_task(str(other), "other") is None
    assert reader.batches(str(other))[0]["data"]["experience_ids"] == ["q:0"]
    assert reader.cards(str(other))[0].identity == "q:0"
    assert "-old" in skill_diff(skill(0, "old\n"), skill(1, "new\n"))


def test_e1_external_card_path_is_read_without_rewriting(tmp_path):
    r = experiment(tmp_path)
    external = tmp_path / "old-recovery/job/agent/trajectory.json"
    write(external, {"steps": [{"step_id": 1, "message": "external"}]})
    write(r / "evolution/round-1/cards/0.json", {"task_id": 0, "experience_id": "old:0",
          "failed_trajectories": [str(external)]})
    reader = Reader(tmp_path)
    trace = reader.traces(str(r), "0")[0]
    assert trace.path == external
    assert reader.trace_detail(trace)["steps"][0]["message"] == "external"


def test_result_partial_json_stays_unknown(tmp_path):
    r = experiment(tmp_path)
    p = trial(r, 0, 1, 1, 1)
    write(r / "slots/00-1/record.json", {"task_id": 0, "attempt_index": 1, "trajectory_path": str(p), "status": "pending"})
    (p.parent.parent / "result.json").write_text('{"verifier_result":')
    reader = Reader(tmp_path)
    d = reader.trace_detail(reader.traces(str(r), "0")[0])
    assert d["reward"] is None
    assert "暂不可读" in d["result_error"]
    (p.parent.parent / "result.json").write_text('{"verifier_result":{"rewards":{"reward":1}},"exception_info":null,')
    d = reader.trace_detail(reader.traces(str(r), "0")[0])
    assert d["reward"] is None and "暂不可读" in d["result_error"]


def test_e10_bare_index_uses_audited_reward_and_preserves_unknown(tmp_path):
    registration = tmp_path / "tb21-e10-qwen-bare-262144-20261014"
    job = tmp_path / "jobs/tb21_e10_qwen_bare_262144_20261014"
    write(registration / "full_config.json", {"jobs_dir": str(job.parent), "job_name": job.name})
    for name in ["task__a", "task__b"]:
        write(job / name / "result.json", {"task_name": "task",
              "verifier_result": {"rewards": {"reward": 0}}})
        write(job / name / "agent/trajectory.json", {"steps": []})
    write(registration / "execution_audit.json", {"rows": [
        {"trial": "task__a", "result": str(job / "task__a/result.json"), "reward": 1}]})
    reader = Reader(tmp_path)
    run = reader.runs[str(job)]
    assert "E10" in run.label
    assert [row["reward"] for row in run.raw] == [1, None]
    assert [row["accepted"] for row in run.raw] == [True, False]


def test_gradio_callbacks_keep_task_trace_card_and_compare_consistent(tmp_path):
    import pytest
    pytest.importorskip("gradio")
    from skillexpand.reader.app import build_demo

    r = experiment(tmp_path)
    p = trial(r, 0, 1, 1, 1)
    write(r / "slots/00-1/record.json", {"task_id": 0, "attempt_index": 1, "status": "valid", "reward": 1, "trajectory_path": str(p)})
    cpath = r / "discovery/results/0.json"
    write(cpath, {"experience_id": "c:0", "task_id": 0, "l1_trials": [{"index": 1, "trajectory": str(p)}],
          "experience_card": {"task": {"task_id": 0}, "evidence": [{"id": "t1:e8", "trial": 1, "step_id": 8}]}})
    app = build_demo(tmp_path)
    funcs = {f.fn.__name__: f for f in app.fns.values() if f.fn}
    for name, args in [("populate", (str(r), "0", str(r))),
                       ("open_card", (str(r), str(cpath), str(r))),
                       ("jump_evidence", (str(r), str(cpath), f"{r}\n{p}", "0", str(r))),
                       ("render_compare", (str(r), "0", str(p), str(r), str(p)))]:
        fn = funcs[name]
        result = fn.fn(*args)
        assert len(result) == len(fn.outputs), name
    jumped = funcs["jump_evidence"].fn(str(r), str(cpath), f"{r}\n{p}", "0", str(r))
    assert any(isinstance(x, str) and "已定位" in x for x in jumped)
    compared = funcs["render_compare"].fn(str(r), "0", str(p), str(r), str(p))
    assert "reward=1" in compared[0]
    app.close()
