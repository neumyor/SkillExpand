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
    from materialize_terminalbench_library import main as materialize
    skill = {'skill_id': 'terminalbench.family-p001', 'family_id': 'family-p001',
             'name': 'Inspect', 'description': 'Inspect outputs',
             'trigger_conditions': ['A tool produced output'], 'body': '1. Inspect the output.'}
    request = tmp_path / '000.request.json'
    payload = request_payload('DEEPSEEK_up5zdj', 'EXPERIENCE CARDS:\n' + json.dumps(rows))
    request.write_text(json.dumps(payload))
    completion = tmp_path / '000.completion.json'
    completion.write_text(json.dumps({'choices': [{'message': {'content': json.dumps({'skills': [skill]})}}]}))
    proposal = tmp_path / 'proposal.json'
    proposal.write_text(json.dumps({'skills': [skill], 'generation_request': str(request),
                                    'generation_payload': payload}))
    from unittest.mock import patch
    with patch.object(sys, 'argv', ['materialize', '--run-dir', str(tmp_path), '--proposal', str(proposal)]):
        assert materialize() == 0
    audit = json.loads((tmp_path / 'generation_source_audit.json').read_text())
    manifest = json.loads((tmp_path / 'library_manifest.json').read_text())
    assert audit['passed'] and audit['actual_request_cards'] == manifest['source_cards'] == 178
    assert audit['source_card_ids'] == manifest['source_card_ids']
    assert len(audit['skill_provenance']['terminalbench.family-p001@v0']) == 178
    request.write_text(json.dumps({**payload, 'model': 'different-model'}))
    with patch.object(sys, 'argv', ['materialize', '--run-dir', str(tmp_path), '--proposal', str(proposal)]):
        with pytest.raises(ValueError, match='request differs'):
            materialize()
    cards[-1]['source_card_id'] = cards[0]['source_card_id']
    path.write_text(json.dumps(cards))
    with pytest.raises(RuntimeError, match='178'):
        _cards(tmp_path)
