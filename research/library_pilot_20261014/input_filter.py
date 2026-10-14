"""Full legal tool observations for package evolution, without size projection."""
import re

_RESTRICTED = re.compile(r'/tests|verifier|reward\.txt|test[-_]stdout', re.IGNORECASE)


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from strings(key)
            yield from strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from strings(item)


def project_tool_results(trace):
    observations = []
    for index, step in enumerate(trace['steps']):
        calls = step.get('tool_calls') or []
        observation = step.get('observation')
        if not calls and observation is None:
            continue
        results = (observation or {}).get('results', [])
        restricted = any(_RESTRICTED.search(text)
                         for text in strings([calls, results]))
        item = {'step_index': index, 'step_id': step.get('step_id'),
                'tool_functions': [call.get('function_name') for call in calls],
                'tool_calls_count': len(calls), 'observation_present': observation is not None,
                'results_count': len(results), 'results': [],
                'content_status': 'restricted_evaluation_output_removed' if restricted else 'full_untruncated'}
        if not restricted:
            for result_index, result in enumerate(results):
                content = result.get('content', '')
                if not isinstance(content, str):
                    raise ValueError('Tool content must be a string')
                item['results'].append({'result_index': result_index, 'content': content,
                       'original_characters': len(content), 'truncated': False,
                       'head_characters': len(content), 'tail_characters': 0})
        observations.append(item)
    return observations
