import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from propose_terminalbench_library import _cards, cached_completion, request_payload
import pytest


def test_cache_requires_exact_request(tmp_path):
    path = tmp_path / '000.completion.json'
    path.write_text(json.dumps({'choices': [{'message': {'content': 'ready'}}]}))
    expected = request_payload('DEEPSEEK_up5zdj', '178 cards')
    assert cached_completion(path, expected) is None
    request = tmp_path / '000.request.json'
    request.write_text(json.dumps(expected))
    assert cached_completion(path, expected) == 'ready'
    for field, changed in [('model', 'qwen'), ('messages', [{'role': 'user', 'content': '89 cards'}]), ('max_tokens', 32)]:
        assert cached_completion(path, {**expected, field: changed}) is None


def test_178_cards_preserve_unknown_and_reject_duplicate(tmp_path):
    cards = []
    for model in ['deepseek-v4-flash-0731', 'qwen3.6-flash-distill']:
        result = tmp_path / (model + '.result.json')
        trajectory = tmp_path / (model + '.trajectory.json')
        result.write_text(json.dumps({'config': {'agent': {'model_name': 'openai/' + model}}}))
        trajectory.write_text(json.dumps({'steps': [{'message': 'observed'}]}))
        for task in range(89):
            cards.append({'source_card_id': f'{model}:{task}', 'experience_id': f'{model}:{task}:experience',
                'source_model': model, 'task_id': task,
                'experience_card': {'task': {'text': 'actual instruction'}},
                'attempts': [{'attempt_index': a, 'result_path': str(result),
                    'trajectory_path': str(trajectory), 'reward': None if task == 0 else 0}
                    for a in [1, 2, 3]]})
    path = tmp_path / 'source_cards.json'
    path.write_text(json.dumps(cards))
    rows = _cards(tmp_path)
    assert len(rows) == 178 and rows[0]['task'] == 'actual instruction'
    assert rows[0]['reward'] is None and rows[1]['reward'] is False
    cards[-1]['source_card_id'] = cards[0]['source_card_id']
    path.write_text(json.dumps(cards))
    with pytest.raises(RuntimeError, match='178'):
        _cards(tmp_path)
