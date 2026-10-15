"""Final offline audit; no execution or prompt modification."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys


def read(path):
    return json.loads(Path(path).read_text())


def audit(root):
    root = Path(root)
    sys.path[:0] = [str(root), str(root / 'code')]
    from skillexpand import schema as S
    from skillexpand.skill_package import entrypoint_content
    from summarize_tb21_empirical import validate_trial
    from evidence import evaluate
    spec = importlib.util.spec_from_file_location('_read_native_runtime', root / 'runtime.py')
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    library = read(root / 'frozen/library.json')
    catalog = [{'skill_id': row['skill_id'], 'description': row['description']}
               for row in sorted(library, key=lambda row: row['skill_id'])]
    expected_files = {}
    for value in library:
        skill = S.from_dict(S.Skill, value)
        contents = {'SKILL.md': entrypoint_content(skill, package=skill.package_mode)}
        if skill.package_mode:
            contents.update(skill.resources)
        expected_files.update({skill.skill_id + '/' + path: text for path, text in contents.items()})
    errors, trials, identities, common = [], [], set(), []
    bindings = 0
    for folder in [root] + [root / name for name in ('dev-v1', 'dev-v2', 'acceptance') if (root / name).exists()]:
        for row in read(folder / 'bindings.json'):
            bindings += 1
            if (folder / row['current']).read_bytes() != (folder / row['snapshot']).read_bytes():
                errors.append(str(folder / row['current']) + ': frozen bytes changed')
    for name in ('dev-v1', 'dev-v2', 'acceptance'):
        phase = root / name
        if not phase.exists():
            continue
        manifest = read(phase / 'manifest.json')
        schedule = read(phase / 'frozen/schedule.json')
        template = read(root / 'frozen/executor_template.json')
        for scheduled in schedule:
            slot = phase / name / 'slots/evolved_autonomous' / scheduled['task'] / f"{scheduled['attempt']:02d}"
            record = read(slot / 'record.json')
            row = {'phase': name, 'task': scheduled['task'], 'attempt': scheduled['attempt'],
                   'official_reward': None, 'reading_status': 'unresolved', 'errors': []}
            trials.append(row)
            actual = {str(path.relative_to(slot / 'skills')): path.read_text()
                      for path in (slot / 'skills').rglob('*') if path.is_file()}
            if actual != expected_files or read(slot / 'catalog.json') != catalog:
                row['errors'].append('complete mounted library drift')
            if any(slot.glob('selector_*')):
                row['errors'].append('external selector artifacts')
            config = read(slot / 'harbor_config.json')
            expected_config = runtime.build_config(template, phase, slot, scheduled['task'], slot / 'skills', 'autonomous')
            if config != expected_config:
                row['errors'].append('runtime configuration drift')
            common.append(config['agents'][0])
            result_path = Path(record['result_path']) if record.get('result_path') else None
            if record['status'] == 'valid' and result_path:
                official, reward, _ = validate_trial(result_path, scheduled['task'])
                if official['config']['agent'] != config['agents'][0]:
                    # Harbor records the import class instead of the configured short name.
                    if {**official['config']['agent'], 'name': config['agents'][0]['name']} != config['agents'][0]:
                        row['errors'].append('official agent drift')
                for key in ('environment', 'verifier'):
                    if official['config'][key] != config[key]:
                        row['errors'].append('official ' + key + ' drift')
                agent = result_path.parent / 'agent'
                requests = [json.loads(line) for line in (agent / 'raw_model_requests.jsonl').read_text().splitlines()]
                text = requests[0]['messages'][0]['content']
                paths = read(agent / 'loading_query_paths.json')
                checks = {'tail': text.endswith(manifest['autonomous_user_query_tail']),
                    'catalog': json.dumps(catalog, ensure_ascii=False) in text,
                    'paths': paths['all_exist'] and set(paths['entrypoints']) ==
                             {'/opt/openclaw-skills/' + skill['skill_id'] + '/SKILL.md' for skill in library},
                    'no_body_injection': not any(content.strip() and content.strip() in text for content in expected_files.values())}
                effective = read(agent / 'effective_agent_config.json')
                checks['effective_runtime'] = all(effective.get(key) == value for key, value in {
                    'max_input_tokens': 262144, 'max_output_tokens': 65536,
                    'enable_summarize': True, 'proactive_summarization_threshold': 8000,
                    'api_base': 'https://llm-center.modelbest.co/v1', 'loading_mode': 'autonomous'}.items())
                checks['actual_parameters'] = all(all(request.get(key) == value for key, value in {
                    'model': 'qwen3.6-flash-distill', 'max_tokens': 65536, 'top_p': .95,
                    'top_k': 20, 'enable_thinking': True}.items()) and
                    request.get('chat_template_kwargs', {}).get('preserve_thinking') is True for request in requests)
                proof = evaluate(read(agent / 'trajectory.json'), requests, library)
                strict_reads = []
                for candidate in proof['qualifying_reads']:
                    for returned in candidate['returned_to_model']:
                        messages = requests[returned['request_index']]['messages']
                        last = messages[-1]
                        if last.get('role') == 'user' and all(line in last.get('content', '') for line in candidate['body_lines']):
                            strict_reads.append({**candidate, 'first_body_request_index': returned['request_index']})
                            break
                identity = read(result_path.parent / 'snapshot.json')['source_sandbox_id']
                checks['fresh_identity'] = identity not in identities and identity not in read(phase / 'frozen/historical_sandbox_ids.json')
                identities.add(identity)
                row.update(official_reward=reward, checks=checks, first_task_operation=proof['first_task_operation'],
                    reads=strict_reads, sandbox_id=identity,
                    reading_status='pass' if strict_reads and all(checks.values()) else 'fail')
            errors.extend(row['errors'])
    normalized = []
    for agent in common:
        value = json.loads(json.dumps(agent))
        value['kwargs'].pop('loading_manifest', None)
        normalized.append(value)
    if normalized and any(value != normalized[0] for value in normalized):
        errors.append('common executor parameters differ')
    reservations = list(root.glob('*/*/slots/*/*/*/reserved.json'))
    if len(reservations) != len(trials) or len(trials) > 9:
        errors.append('execution budget or extra slots')
    selection = read(root / 'selection.json')
    acceptance_manifest = read(root / 'acceptance/manifest.json')
    if (selection['prompt'] != acceptance_manifest['prompt'] or
            selection['text'] != acceptance_manifest['autonomous_user_query_tail'] or
            selection['text'] != read(root / 'frozen/prompts.json')[selection['prompt']]):
        errors.append('selected prompt differs from acceptance')
    if any(read(path)['at'] <= selection['at'] for path in
           (root / 'acceptance/acceptance/slots').rglob('reserved.json')):
        errors.append('acceptance reserved before prompt selection')
    acceptance = [row for row in trials if row['phase'] == 'acceptance']
    summary = {'audit_passed': not errors, 'errors': errors, 'frozen_bindings': bindings,
        'total_executions': len(trials), 'unique_sandbox_ids': len(identities),
        'selected_prompt': selection['prompt'], 'prompt': selection['text'],
        'reading_gate_passed': not errors and len(acceptance) == 5 and all(row['reading_status'] == 'pass' for row in acceptance),
        'trials': trials}
    (root / 'final_audit.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: value for key, value in summary.items() if key not in ('trials', 'prompt')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    audit(parser.parse_args().root)
