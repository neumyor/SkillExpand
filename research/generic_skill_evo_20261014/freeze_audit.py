"""Bind semantically reviewed generation and evaluation artifacts by exact bytes."""
import argparse
import json
from pathlib import Path

from generate import project
from skillexpand import schema as S
from skillexpand.l2.editor import SkillEditor
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent


def read(path):
    return json.loads(path.read_text())


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main(number):
    directory = ROOT / f'development/candidate-{number:03d}'
    provenance = read(directory / 'provenance.json')
    frozen = read(ROOT / 'frozen/input.json')
    evidence = read(ROOT / 'frozen/projected_input.json')
    require(project(frozen) == evidence, 'Projection drift')
    base = S.from_dict(S.Skill, frozen['original_skill'])
    feedback_paths = []
    if number > 1:
        previous = ROOT / f'development/candidate-{number-1:03d}'
        base = S.from_dict(S.Skill, read(previous / 'skill.json'))
        feedback_paths = sorted(previous.glob('dev-batch-*/trial-*/feedback.json'))
    feedback = [read(path) for path in feedback_paths]
    messages = SkillEditor(None, SimpleNamespace(version=0)).build_package_prompt(base, evidence, feedback)
    actual = read(directory / 'request.json')
    expected_messages = [{'role': 'system' if message.type == 'system' else 'user',
                          'content': message.content} for message in messages]
    require(actual == {'model': frozen['method_model'], 'stream': True,
                       'max_tokens': 65536, 'enable_thinking': True,
                       'messages': expected_messages}, 'Precise request drift')
    candidate = S.from_dict(S.Skill, read(directory / 'skill.json'))
    require(candidate.description == frozen['original_skill']['description'], 'Routing drift')
    attempt = Path(provenance['generation_attempt'])
    if not attempt.exists():
        attempt = directory / 'generation_attempts' / attempt.name
    completion = read(attempt / 'completion.json')
    from skillexpand.l1.family_discovery import _extract_json
    generated = _extract_json(completion['choices'][0]['message']['content'])
    require(generated['body'] == candidate.body and generated['resources'] == candidate.resources,
            'Generated package drift')
    require(read(attempt / 'outcome.json')['candidate']['skill'] == read(directory / 'skill.json'),
            'Full candidate origin drift')
    roles = {
        'skill': directory / 'skill.json', 'request': directory / 'request.json',
        'frozen_input': ROOT / 'frozen/input.json', 'projected_input': ROOT / 'frozen/projected_input.json',
        'executor': ROOT / 'frozen/executor.json', 'protocol': ROOT / 'frozen/protocol.json',
        'generation_module': ROOT / 'generate.py', 'evaluator': ROOT / 'evaluate.py',
        'native_editor': ROOT / 'code/skillexpand/l2/editor.py',
        'schema': ROOT / 'code/skillexpand/schema.py',
        'parser': ROOT / 'code/skillexpand/l1/family_discovery.py',
        'relay': ROOT / 'code/skillexpand/runtime/llm_relay.py',
        'benchmark_adapter': ROOT / 'code/skillexpand/benchmarks/terminalbench.py',
        'package_renderer': ROOT / 'code/skillexpand/skill_package.py',
        'semantic_review': ROOT / 'genericity_audit.md',
        'completion': attempt / 'completion.json',
        'sse': attempt / 'response.sse',
        'actual_request': attempt / 'request.json',
        'outcome': attempt / 'outcome.json',
        'provenance': directory / 'provenance.json',
        'initial_request': ROOT / 'frozen/initial_request.json',
        'audit_builder': ROOT / 'freeze_audit.py',
    }
    roles.update({f'feedback_{index}': path for index, path in enumerate(feedback_paths)})
    bindings = []
    for role, path in roles.items():
        snapshot = directory / 'audit_snapshots' / (role + '.txt')
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        if snapshot.exists():
            require(snapshot.read_bytes() == path.read_bytes(), 'Audited bytes changed: ' + role)
        else:
            snapshot.write_bytes(path.read_bytes())
        bindings.append({'role': role, 'current': str(path.relative_to(ROOT)),
                         'snapshot': str(snapshot.relative_to(ROOT))})
    marker = {'passed': True, 'candidate': number, 'bindings': bindings,
              'origin_verified': True, 'precise_request_verified': True,
              'semantic_review': 'genericity_audit.md',
              'old_targeted_results_used': False}
    (directory / 'preaccept_audit.json').write_text(json.dumps(marker, indent=2) + '\n')
    print(json.dumps({'candidate': number, 'passed': True, 'bindings': len(bindings)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--candidate', type=int, default=1)
    main(parser.parse_args().candidate)
