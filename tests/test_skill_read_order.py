import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location('read_evidence', Path(__file__).resolve().parents[1] /
    'research/skill_read_prompt_20261015/evidence.py')
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)
BODY = 'Inspect the public input and confirm its required format before choosing an operation.\nVerify the resulting output against the public requirements before declaring completion.'
PATH = '/opt/openclaw-skills/demo/SKILL.md'
LIBRARY = [{'skill_id': 'demo', 'body': BODY}]


def step(command, output, identifier):
    return {'step_id': identifier, 'tool_calls': [{'function_name': 'bash_command',
        'arguments': {'keystrokes': command}}], 'observation': {'results': [{'content': output}]}}


def test_read_must_return_body_to_model_before_task_operation():
    read = step('cat ' + PATH + '\n', BODY, 1)
    task = step('git status\n', 'clean', 2)
    requests = [{'messages': [{'role': 'user', 'content': 'catalog'},
        {'role': 'user', 'content': BODY}]}]
    assert evidence.evaluate({'steps': [read, task]}, requests, LIBRARY)['reading_order_pass']
    assert not evidence.evaluate({'steps': [task, read]}, requests, LIBRARY)['reading_order_pass']
    assert not evidence.evaluate({'steps': [read, task]}, [], LIBRARY)['reading_order_pass']


def test_metadata_path_or_same_batch_task_cannot_pass():
    requests = [{'messages': [{'role': 'user', 'content': 'catalog'}, {'role': 'user', 'content': BODY}]}]
    for command, output in [('ls ' + PATH, PATH), ('cat ' + PATH, '---\nname: demo\n---'),
                            ('cat ' + PATH + ' && git status', BODY)]:
        assert not evidence.evaluate({'steps': [step(command, output, 1)]}, requests, LIBRARY)['reading_order_pass']
