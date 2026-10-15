"""Frozen factorial schedule and conservative loading evidence extraction."""
from collections import Counter
from pathlib import Path
import json
import random

CELLS = {'old_single': ('old', 'single'), 'evolved_single': ('evolved', 'single'),
         'old_autonomous': ('old', 'autonomous'),
         'evolved_autonomous': ('evolved', 'autonomous')}


def schedule(tasks):
    rows = []
    generator = random.Random(20261015)
    for attempt in range(1, 4):
        names = list(tasks)
        generator.shuffle(names)
        for task in names:
            cells = list(CELLS)
            generator.shuffle(cells)
            rows.extend({'cell': cell, 'task': task, 'attempt': attempt} for cell in cells)
    return rows


def slot_path(root, phase, row):
    return Path(root) / phase / 'slots' / row['cell'] / row['task'] / f"{row['attempt']:02d}"


def collect_loading_evidence(trace, events, manifest):
    """File ACCESS establishes a read; observed content and invocation remain separate."""
    files = manifest['files']
    accesses = []
    for event in events:
        if event.get('kind') == 'access' and event.get('path') in files:
            info = files[event['path']]
            accesses.append({**event, 'skill_id': info['skill_id'],
                             'resource': info['relative_path'],
                             'evidence': 'kernel_file_access_not_proof_of_model_consumption'})
    steps = []
    for index, step in enumerate(trace.get('steps', [])):
        calls = step.get('tool_calls') or []
        results = (step.get('observation') or {}).get('results', [])
        observed = '\n'.join(result.get('content', '') for result in results)
        arguments = json.dumps([call.get('arguments', {}) for call in calls], ensure_ascii=False)
        references, content_matches = [], []
        for path, info in files.items():
            if path in arguments:
                references.append({'path': path, 'skill_id': info['skill_id'],
                                   'resource': info['relative_path']})
            content = info['content'].strip()
            # Matching multiple lines avoids treating an echoed path as reading the body.
            lines = [line.strip() for line in content.splitlines() if len(line.strip()) >= 30]
            matches = [line for line in lines if line in observed]
            if len(matches) >= min(3, len(lines)) and len(matches) >= 2:
                content_matches.append({'path': path, 'skill_id': info['skill_id'],
                                        'resource': info['relative_path'],
                                        'matching_lines': len(matches),
                                        'evidence': 'content_lines_in_terminal_observation'})
        if references or content_matches:
            steps.append({'step_index': index, 'step_id': step.get('step_id'),
                          'timestamp': step.get('timestamp'), 'path_references': references,
                          'observed_contents': content_matches,
                          'subsequent_step_ids': [s.get('step_id') for s in trace.get('steps', [])[index + 1:index + 4]],
                          'resource_execution': 'requires_manual_command_and_observation_review'})
    read_ids = sorted({row['skill_id'] for row in accesses})
    body_ids = sorted({row['skill_id'] for row in accesses if row['resource'] == 'SKILL.md'})
    return {'directory_visible': sorted(manifest['skill_ids']), 'actual_file_accesses': accesses,
            'read_skill_ids': read_ids, 'body_read_ids': body_ids,
            'resource_only_ids': sorted(set(read_ids) - set(body_ids)),
            'joint_read_set': read_ids, 'steps': steps,
            'attribution': 'joint trajectory only; no per-skill success credit',
            'access_counts': dict(Counter(row['skill_id'] for row in accesses))}
