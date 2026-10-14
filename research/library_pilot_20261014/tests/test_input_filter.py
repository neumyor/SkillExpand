import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('library_full_filter', Path(__file__).parents[1] / 'input_filter.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_full_middle_and_all_observations_survive_both_former_limits():
    contents = ['a' * 30000 + 'MIDDLE-EVIDENCE' + 'z' * 30000 for _ in range(3)]
    trace = {'steps': [{'step_id': i, 'tool_calls': [{'function_name': 'terminal',
                         'arguments': {'command': 'cat public.csv'}}],
                         'observation': {'results': [{'content': text}]}}
                        for i, text in enumerate(contents)]}
    projected = module.project_tool_results(trace)
    assert [row['results'][0]['content'] for row in projected] == contents
    assert all(not row['results'][0]['truncated'] for row in projected)


def test_restricted_sibling_step_is_removed_without_legal_size_loss():
    trace = {'steps': [{'tool_calls': [{'arguments': {'command': 'cat /tests/test.sh'}}],
                       'observation': {'results': [{'content': 'hidden'}, {'content': 'sibling'}]}}]}
    assert module.project_tool_results(trace)[0]['results'] == []
