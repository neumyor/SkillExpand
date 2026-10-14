"""Rebuild tool memory and exercise its consumer without changing old runs."""
import argparse
import json
from pathlib import Path

from skillexpand.benchmarks.atif import load_events
from skillexpand.l1 import learning as L, protocol as P


def rebuild(source, output, task_names=()):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output == source or source in output.parents:
        raise ValueError('Repair output must be outside the original run')
    output.mkdir(parents=True, exist_ok=False)
    summaries = []
    for result_path in sorted(source.rglob('result.json')):
        result = json.loads(result_path.read_text())
        task = result.get('task_name')
        if not task or (task_names and task not in task_names):
            continue
        trajectory = result_path.parent / 'agent/trajectory.json'
        events, status = load_events(trajectory)
        reward = (result.get('verifier_result') or {}).get('rewards', {}).get('reward')
        trial = {'index': 1, 'phase': 'autonomous', 'status': 'completed',
                 'success': reward == 1, 'termination': (result.get('exception_info') or {}).get('exception_type') or 'verifier',
                 'trajectory': str(trajectory), 'trajectory_parse': status, 'events': events}
        relative = result_path.relative_to(source)
        instruction = task
        tasks_file = source / 'tasks.json'
        if tasks_file.exists():
            tasks = json.loads(tasks_file.read_text())
            if isinstance(tasks, dict):
                tasks = tasks.get('tasks', [])
            instruction = next((t['instruction'] for t in tasks if t.get('task_name') == task), task)
        card = L.card(task, instruction, [trial], {'status': 'valid', 'claims': []},
                      'external_harbor', str(relative), len,
                      evidence=P.evidence(instruction, [trial]), benchmark='terminalbench')
        view = P.projection(card)
        # Check the saved card against every original tool result, including tails.
        assert len(card['evidence']) == len(events)
        for row, event in zip(card['evidence'], events):
            assert row['observation'] == event['observation']
            assert row['action'] == event['action'] and row['origin'] == event['source']
        assert len(json.dumps(view['evidence'], ensure_ascii=False)) <= 24002
        destination = output / relative.parent
        destination.mkdir(parents=True, exist_ok=True)
        for name, payload in [('memory_card.json', card), ('model_view.json', view)]:
            (destination / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
        summaries.append({'task': task, 'source_result': str(result_path), 'source_trajectory': str(trajectory),
                          'parse': status, 'observations': len(events), 'visible_observations': len(view['evidence']),
                          'omitted_evidence': view.get('omitted_evidence', 0),
                          'omitted_observation_chars': view.get('omitted_observation_chars', 0),
                          'original_reward': reward, 'card': str(destination / 'memory_card.json')})
    covered = {row['task'] for row in summaries if row['parse']['status'] == 'parsed' and row['observations']}
    passed = bool(summaries) and (set(task_names) <= covered if task_names else
                                all(row['parse']['status'] == 'parsed' for row in summaries))
    manifest = {'memory_format': 'terminalbench-tools-v1', 'source': str(source),
                'original_skills_and_scores': 'generated_with_old_memory; not_recomputed',
                'validation': 'full_tool_output_and_source_equality; bounded_model_projection',
                'passed': passed, 'records': summaries}
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--tasks', nargs='*', default=[])
    args = parser.parse_args()
    result = rebuild(args.source, args.output, args.tasks)
    print(json.dumps({'passed': result['passed'], 'records': len(result['records']), 'output': str(args.output)}))
    raise SystemExit(0 if result['passed'] else 1)
