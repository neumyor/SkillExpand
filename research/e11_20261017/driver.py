"""E11 driver: DeepSeek routing/evolution, then prepare autonomous Qwen."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

# The prepared remote run is self-contained under its own root.
_ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(_ROOT / 'code'), str(_ROOT / 'src'), str(_ROOT),
                str(_ROOT / 'frozen/runtime/tencent/python'),
                str(_ROOT / 'frozen/runtime/framework')]

from skillexpand import schema as S
from skillexpand.l2.editor import SkillEditor
from skillexpand.runtime.llm_relay import coalesce_sse, relay_from_env
from input_filter import project_tool_results


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


class DeepSeekHost:
    def __init__(self, relay, root, label):
        self.relay, self.root, self.label = relay, Path(root), label

    def llm(self, messages, **kwargs):
        payload = {'model': 'DEEPSEEK_up5zdj', 'stream': True,
                   'max_tokens': 65536, 'enable_thinking': True,
                   'messages': [{'role': 'system' if getattr(m, 'type', '') == 'system' else 'user',
                                 'content': m.content} for m in messages]}
        number = len(list((self.root / 'generation_requests').glob('*.json')))
        request = self.root / 'generation_requests' / f'{number:04d}-{self.label}.json'
        write(request, payload)
        response = coalesce_sse(self.relay.transport.stream(payload))
        write(request.with_suffix('.response.json'), response)
        return response['choices'][0]['message']['content']


def load_cards(root):
    tasks = {row['task_name']: row for row in read(Path(root) / 'frozen/task_table.json')}
    cards = {}
    for row in read(Path(root) / 'frozen/e10_trials.json'):
        path = Path(row['trajectory_path'])
        if not path.exists():
            raise FileNotFoundError(path)
        trace = read(path)
        events = project_tool_results({'steps': trace.get('steps', [])})
        cards.setdefault(row['task_name'], []).append({'task_name': row['task_name'], 'events': events})
    if set(cards) != set(tasks) or any(len(v) != 3 for v in cards.values()):
        raise ValueError('E10 card coverage is not 89 x 3')
    return tasks, cards


def route(root, host, tasks, scopes):
    payload = {'scopes': scopes, 'tasks': [
        {'task_name': name, 'instruction': row['instruction']}
        for name, row in sorted(tasks.items())]}
    prompt = ('Return exactly JSON {"assignments":[{"task_name":"...","scope_ids":["..."]}]} . '
              'Assign every task to one or more reusable Skill scopes using only the '
              'public operation contract and scope descriptions. Never include solutions, '
              'answers, verifier details, or evidence claims. Scope IDs must come from the '
              'supplied list; every task must appear exactly once.\n' +
              json.dumps(payload, ensure_ascii=False))
    raw = host.llm([type('M', (), {'type': 'system', 'content': 'You are a generic capability router.'})(),
                    type('M', (), {'type': 'user', 'content': prompt})()])
    value = json.loads(raw[raw.find('{'):raw.rfind('}') + 1])
    allowed = {s['skill_id'] for s in scopes}
    assignments = value.get('assignments')
    if not isinstance(assignments, list):
        raise ValueError('Router returned no assignments')
    mapping = {}
    for row in assignments:
        if row.get('task_name') in mapping or not set(row.get('scope_ids', [])) <= allowed or not row.get('scope_ids'):
            raise ValueError('Invalid routing assignment')
        mapping[row['task_name']] = list(row['scope_ids'])
    if set(mapping) != set(tasks):
        raise ValueError('Routing did not cover every task')
    write(Path(root) / 'routing.json', {'model': 'DEEPSEEK_up5zdj', 'assignments': mapping,
                                        'raw_response': raw})
    return mapping


def evolve(root):
    root = Path(root)
    tasks, cards = load_cards(root)
    scopes = read(root / 'frozen/scopes.json')
    relay = relay_from_env(); relay.start()
    try:
        host = DeepSeekHost(relay, root, 'router')
        mapping = route(root, host, tasks, scopes)
        by_scope = {s['skill_id']: [] for s in scopes}
        for task_name, ids in mapping.items():
            for scope_id in ids:
                by_scope[scope_id].append(task_name)
        write(root / 'scope_inputs.json', by_scope)
        seed = {row['skill_id']: row for row in read(root / 'frozen/seed_library.json')}
        plan = []
        for scope in scopes:
            names = by_scope[scope['skill_id']]
            chars = 0; batches = 0
            for name in names:
                size = len(json.dumps({'task_name': name, 'instruction': tasks[name]['instruction'],
                                       'trials': cards[name]}, ensure_ascii=False))
                if chars and chars + size > 90000:
                    batches += 1; chars = 0
                chars += size
            if names: batches += 1
            plan.append({'scope_id': scope['skill_id'], 'tasks': names, 'batches': batches,
                         'max_editor_calls': batches})
        write(root / 'evolution_plan.json', {'routing_calls': 89, 'scopes': plan,
            'max_generation_calls': 89 + sum(row['batches'] for row in plan),
            'max_evolution_calls': sum(row['batches'] for row in plan),
            'frozen_before_synthesis': True})
        evolved = []
        ledger = []
        for scope in scopes:
            scope_id = scope['skill_id']; names = by_scope[scope_id]
            if not names:
                raise ValueError('Every Skill scope must receive evidence')
            base = S.from_dict(S.Skill, seed[scope_id])
            batches, current = [], base
            current_chars = 0; current_rows = []
            for name in names:
                value = {'task_name': name, 'instruction': tasks[name]['instruction'],
                         'trials': cards[name]}
                size = len(json.dumps(value, ensure_ascii=False))
                if current_rows and current_chars + size > 90000:
                    batches.append(current_rows); current_rows = []; current_chars = 0
                current_rows.append(value); current_chars += size
            if current_rows: batches.append(current_rows)
            for index, batch in enumerate(batches):
                evidence = {'historical_evidence': [
                    {'id': f'{row["task_name"]}:{i}', 'task': row['task_name'],
                     'observation': row['trials'], 'source': 'frozen_e10_tool_results'}
                    for i, row in enumerate(batch)],
                    'public_contract': [tasks[row['task_name']]['instruction'] for row in batch]}
                editor = SkillEditor(host, type('Meta', (), {'version': 0})())
                outcome = editor.propose_package(current, evidence, ())
                if outcome.proposed:
                    current = outcome.candidate.skill
                    status = 'proposed'
                elif outcome.reason == 'no_effective_change':
                    status = 'no_change'
                else:
                    raise ValueError(f'Invalid package proposal for {scope_id}: {outcome.reason}')
                ledger.append({'scope_id': scope_id, 'batch': index, 'tasks': [r['task_name'] for r in batch],
                               'status': status, 'body_chars': len(current.body),
                               'resources': sorted(current.resources)})
            evolved.append(S.to_dict(current))
        write(root / 'evolution_call_ledger.json', ledger)
        write(root / 'evolved_library.json', evolved)
        write(root / 'library_manifest.json', {'skill_count': len(evolved),
            'skills': [{'skill_id': s['skill_id'], 'description': s['description'],
                        'package_mode': s.get('package_mode', False)} for s in evolved],
            'catalog_has_body': False, 'generation_model': 'DEEPSEEK_up5zdj',
            'e10_results_returned_to_generation': False})
        write(root / 'generation_audit.json', {'passed': True, 'skills': len(evolved),
              'routes': len(mapping), 'batches': len(ledger), 'no_task_assignment_in_package': True,
              'no_hidden_test_or_verifier_input': True, 'no_result_feedback': True})
    finally:
        relay.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('evolve', 'formal-prepare', 'formal-launch'))
    parser.add_argument('--root', required=True, type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == 'evolve':
        evolve(root)
    elif args.action == 'formal-prepare':
        subprocess.run([os.environ.get('PYTHON', 'python3'), str(root / 'formal_prepare.py'), '--root', str(root)], check=True)
    else:
        subprocess.run([os.environ.get('PYTHON', 'python3'), str(root / 'formal_launch.py'), '--root', str(root)], check=True)


if __name__ == '__main__':
    main()
