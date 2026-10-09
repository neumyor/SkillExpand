"""Reviewer output contract and recovery from ordinary model formatting errors."""

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from skillexpand.evaluation.validation import PredictedSkillScorer
from skillexpand.reliability.errors import ResponseFormatError
from skillexpand.runtime.json_output import extract_json
from skillexpand.runtime.models.llm import GPTWrapper


def scorer():
    return PredictedSkillScorer(
        SimpleNamespace(benchmark=SimpleNamespace(name="searchqa")),
        SimpleNamespace(fingerprint="panel"), None,
    )


def test_extract_json_recovers_fences_commentary_trailing_comma_and_python_literals():
    expected = {"probability_true": 0.8, "predicted_success": True, "reason": "ok"}
    assert extract_json('```json\n' + json.dumps(expected) + '\n```') == expected
    assert extract_json('Analysis first. {"other": 1}\n' + json.dumps(expected),
                         required_keys=tuple(expected)) == expected
    assert extract_json('json\n{"probability_true":0.8,"predicted_success":true,'
                         '"reason":"literal ,} is fine",}') == {
                             **expected, "reason": "literal ,} is fine"}
    assert extract_json("{'probability_true': 0.8, 'predicted_success': True, "
                         "'reason': 'ok'}") == expected


def test_truncated_json_is_rejected_and_echo_cannot_become_review():
    reviewer = scorer()
    with pytest.raises(ResponseFormatError):
        extract_json('{"probability_true":0.95,"predicted_success":true,'
                      '"reason":"unfinished')
    with pytest.raises(ValueError, match="exactly three"):
        reviewer._parse_response('{"task":"Question: In Northeast Asia:LOUSE",'
                        '"skill":{},"output_schema":{"probability_true":"number",'
                        '"predicted_success":"boolean","reason":"string"}}')


@pytest.mark.parametrize("payload,error", [
    ({"probability_true": 0.8, "predicted_success": False, "reason": "ok"}, "disagrees"),
    ({"probability_true": 1.2, "predicted_success": True, "reason": "ok"}, "outside"),
    ({"probability_true": "0.8", "predicted_success": True, "reason": "ok"}, "numeric"),
    ({"probability_true": 0.8, "predicted_success": True, "reason": "ok", "task": "echo"}, "exactly three"),
])
def test_predicted_review_validates_schema_and_consistency(payload, error):
    with pytest.raises(ValueError, match=error):
        scorer()._parse_response(json.dumps(payload))


def test_reason_at_new_limit_is_accepted():
    payload = {"probability_true": 0.8, "predicted_success": True,
               "reason": "x" * 512}
    assert scorer()._parse_response(json.dumps(payload))["reason"] == payload["reason"]


def test_omit_wire_format_keeps_parser_and_separate_identity(monkeypatch):
    old = scorer()
    monkeypatch.setenv('EXPE_REVIEWER_RESPONSE_FORMAT', 'omit')
    reviewer = scorer()
    assert reviewer.protocol_hash != old.protocol_hash
    calls = []
    def llm(messages, **kwargs):
        calls.append(kwargs)
        return '{"probability_true":0.8,"predicted_success":true,"reason":"ok"}'
    result, attempts = reviewer._review(SimpleNamespace(llm=llm), 'prompt')
    assert attempts == 1 and result['probability_true'] == 0.8
    assert 'response_format' not in calls[0]['request_kwargs']
    with pytest.raises(ValueError, match='disagrees'):
        reviewer._parse_response('{"probability_true":0.8,"predicted_success":false,"reason":"ok"}')


def test_global_thinking_switch_reaches_the_reviewer(monkeypatch):
    monkeypatch.setenv('EXPE_LLM_DISABLE_THINKING', '1')
    reviewer = scorer()
    calls = []
    def llm(messages, **kwargs):
        calls.append(kwargs)
        return '{"probability_true":0.8,"predicted_success":true,"reason":"ok"}'
    reviewer._review(SimpleNamespace(llm=llm), 'prompt')
    assert calls[0]['request_kwargs']['enable_thinking'] is False
    assert calls[0]['request_kwargs']['response_format']['type'] == 'json_schema'


def test_format_retry_preserves_thinking_and_json_schema():
    reviewer = scorer()
    calls = []

    def llm(messages, **kwargs):
        calls.append((messages[-1].content, kwargs))
        if len(calls) == 1:
            return '{"probability_true":0.95,"reason":"truncated'
        return json.dumps({"probability_true": 0.3,
                           "predicted_success": False, "reason": "ambiguous clue"})

    result, attempts = reviewer._review(SimpleNamespace(llm=llm), "task prompt")
    assert attempts == 2
    assert result["probability_true"] == 0.3
    assert calls[1][0] == calls[0][0]
    for _, kwargs in calls:
        assert kwargs["request_kwargs"]["enable_thinking"] is True
        assert kwargs["request_kwargs"]["response_format"]["type"] == "json_schema"
        assert kwargs["stop"] == []


def test_prompt_contains_provider_json_keyword_and_exact_fields():
    prompt = scorer().prompt("Question", SimpleNamespace(description="d", body="b"))
    assert "json" in prompt.lower()
    assert "probability_true" in prompt
    assert "predicted_success" in prompt
    assert "reason" in prompt


def test_provider_request_errors_are_not_retried_as_format_errors():
    reviewer = scorer()
    calls = []

    class InvalidRequestError(Exception):
        pass

    def llm(messages, **kwargs):
        calls.append((messages, kwargs))
        raise InvalidRequestError("invalid response format")

    with pytest.raises(InvalidRequestError):
        reviewer._review(SimpleNamespace(llm=llm), "task prompt")
    assert len(calls) == 1


def test_long_reason_is_accepted_without_changing_prediction():
    reviewer = scorer()
    calls = []

    def llm(messages, **kwargs):
        calls.append((messages[-1].content, kwargs))
        return json.dumps({"probability_true": 0.8,
                           "predicted_success": True,
                           "reason": "x" * (reviewer.REASON_MAX_CHARS + 40)})

    result, attempts = reviewer._review(SimpleNamespace(llm=llm), "task prompt")
    assert attempts == 1
    assert result["probability_true"] == 0.8
    assert result["predicted_success"] is True
    assert len(result["reason"]) == reviewer.REASON_MAX_CHARS + 40


def test_request_kwargs_are_reviewer_local_and_restored():
    client = Mock()
    client.model_kwargs = {"enable_thinking": False}
    seen = []

    def answer(*args, **kwargs):
        seen.append(dict(client.model_kwargs))
        return SimpleNamespace(content=' {"ok":true} ')

    client.side_effect = answer
    with patch('skillexpand.runtime.models.llm.ChatOpenAI', return_value=client):
        wrapper = GPTWrapper('test', 'EMPTY')
        assert wrapper([], request_kwargs={"enable_thinking": True,
                                           "response_format": {"type": "json_object"}}) == '{"ok":true}'
        assert wrapper([]) == '{"ok":true}'
    assert seen[0]["enable_thinking"] is True
    assert seen[0]["response_format"] == {"type": "json_object"}
    assert seen[1] == {"enable_thinking": False}
    assert client.model_kwargs == {"enable_thinking": False}
