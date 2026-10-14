"""Lossless tool evidence from Harbor ATIF trajectories."""
import json
from pathlib import Path


def events(trace, trajectory=None):
    if not isinstance(trace, dict) or not isinstance(trace.get('steps'), list):
        raise ValueError('ATIF steps must be an array')
    rows = []
    for index, step in enumerate(trace['steps'], 1):
        if not isinstance(step, dict):
            raise ValueError('ATIF step must be an object')
        calls = step.get('tool_calls') or []
        observation = step.get('observation')
        if observation is None:
            continue
        results = observation.get('results', []) if isinstance(observation, dict) else [{'content': observation}]
        for result_index, result in enumerate(results):
            content = result.get('content', '') if isinstance(result, dict) else result
            call_id = result.get('tool_call_id') if isinstance(result, dict) else None
            matched = [c for c in calls if call_id and c.get('tool_call_id') == call_id]
            linked = matched if matched else calls
            rows.append({'ref': f'e{index}r{result_index}',
                         'action': json.dumps(linked, ensure_ascii=False) if linked else 'TerminalObservation',
                         'observation': content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
                         'method': bool(linked),
                         'source': {'trajectory_path': str(trajectory) if trajectory else None,
                                    'step_id': step.get('step_id', index), 'step_index': index - 1,
                                    'result_index': result_index, 'tool_call_id': call_id,
                                    'tool_calls': linked, 'association': 'explicit' if matched else 'step'},
                         'environment': {}})
    return rows


def load_events(path):
    try:
        return events(json.loads(Path(path).read_text()), path), {'status': 'parsed'}
    except FileNotFoundError:
        return [], {'status': 'missing', 'path': str(path)}
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return [], {'status': 'invalid', 'path': str(path), 'error': str(exc)}
