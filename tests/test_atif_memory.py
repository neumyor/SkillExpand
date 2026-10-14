import json

from skillexpand.benchmarks.atif import events, load_events
from skillexpand.l1 import learning as L, protocol as P


def trace(output='output'):
    return {'steps': [{'step_id': 8, 'message': 'Unverified assertion',
                      'tool_calls': [{'tool_call_id': 'a', 'function_name': 'bash', 'arguments': {'keystrokes': 'ls'}},
                                     {'tool_call_id': 'b', 'function_name': 'bash', 'arguments': {'keystrokes': 'pwd'}}],
                      'observation': {'results': [{'content': output}]}}]}


def card(rows):
    trials = [{'index': 1, 'phase': 'autonomous', 'status': 'completed', 'success': True,
               'events': rows, 'trajectory_parse': {'status': 'parsed'}}]
    return L.card(0, 'task', trials, {'status': 'valid', 'claims': []}, 'harbor', 'audit', len,
                  evidence=P.evidence('task', trials), benchmark='terminalbench')


def test_shared_output_retains_calls_and_source_without_narration():
    rows = events(trace(), '/raw/trajectory.json')
    assert len(rows) == 1
    assert rows[0]['observation'] == 'output'
    assert 'Unverified assertion' not in json.dumps(rows)
    assert len(rows[0]['source']['tool_calls']) == 2
    assert rows[0]['source']['association'] == 'step'
    origin = card(rows)['evidence'][0]['origin']
    assert origin['step_id'] == 8 and origin['result_index'] == 0
    assert origin['trajectory_path'] == '/raw/trajectory.json'


def test_explicit_call_id_and_empty_output():
    data = trace('')
    data['steps'][0]['observation']['results'][0]['tool_call_id'] = 'b'
    row = events(data)[0]
    assert row['observation'] == ''
    assert row['source']['association'] == 'explicit'
    assert len(row['source']['tool_calls']) == 1
    assert 'pwd' in row['action']


def test_full_card_and_bounded_projection_preserve_tail_and_count_omissions():
    data = trace('x' * 30000 + 'TAIL')
    data['steps'] *= 20
    saved = card(events(data))
    assert len(saved['evidence']) == 20
    assert saved['evidence'][0]['observation'] == 'x' * 30000 + 'TAIL'
    view = P.projection(saved)
    assert len(json.dumps(view['evidence'])) < 25000
    assert view['omitted_evidence'] > 0 and view['omitted_observation_chars'] > 0
    assert view['evidence'][0]['observation'].endswith('TAIL')
    assert not saved['evidence'][0]['observation_truncated']


def test_missing_invalid_and_pure_model_steps(tmp_path):
    path = tmp_path / 'trajectory.json'
    assert load_events(path)[1]['status'] == 'missing'
    path.write_text('{bad')
    assert load_events(path)[1]['status'] == 'invalid'
    assert events({'steps': [{'message': 'narration only'}]}) == []
    path.write_text(json.dumps({'steps': []}))
    assert load_events(path) == ([], {'status': 'parsed'})
