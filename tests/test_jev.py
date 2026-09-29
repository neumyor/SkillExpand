import json
from pathlib import Path
from unittest.mock import patch

from skillexpand import schema as S
from skillexpand.evaluation.jev import JevClient
from skillexpand.evaluation.jev import load_panel_records


def make_skill():
    return S.Skill(
        skill_id="searchqa.family-p001",
        family_id="family-p001",
        version=0,
        name="test",
        description="A routing description.",
        body="1. Search the task.\n2. Submit the answer.",
    )


def test_jev_client_reads_true_probability_and_applies_threshold():
    response = {
        "answers": {
            "success": {
                "choice": "true",
                "confidence": 0.62,
                "probabilities": {"false": 0.38, "true": 0.62},
            }
        }
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps(response).encode()

    with patch("skillexpand.evaluation.jev.urlopen", return_value=FakeResponse()):
        judged = JevClient("http://jev", threshold=0.6).judge("task", make_skill())

    assert judged["probability_true"] == 0.62
    assert judged["predicted_success"] is True
    assert judged["threshold"] == 0.6


def test_panel_loader_rejects_duplicate_skill_task_keys(tmp_path: Path):
    path = tmp_path / "panel.jsonl"
    row = {"skill_key": "searchqa.family-p001@v0", "task_id": 1, "success": True}
    path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")

    try:
        load_panel_records(path)
    except ValueError as exc:
        assert "duplicate actual panel row" in str(exc)
    else:
        raise AssertionError("duplicate panel rows must be rejected")
