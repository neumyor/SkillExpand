"""Create the one-cell autonomous 89 x 3 formal schedule after evolution."""
import argparse
import json
from pathlib import Path


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def catalog_suffix(library):
    rows = [{'skill_id': row['skill_id'], 'description': row['description']} for row in library]
    return (
        '\n\nAvailable Skill catalog (id and description only):\n'
        + json.dumps(rows, ensure_ascii=False)
        + '\nEach Skill body is at /opt/openclaw-skills/<skill_id>/SKILL.md. '
        'Use existing terminal commands to read it; resources are relative to that file. '
        'You decide whether and when to read any Skill or resource. You may read none, '
        'one or multiple Skills, including another Skill later during execution. '
        'Catalog visibility does not mean the Skill contents have been read.'
        '\n\n你可以读取 /opt/openclaw-skills 下的 skill。'
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    root = parser.parse_args().root.resolve()
    library = json.loads((root / 'evolved_library.json').read_text())
    if len(library) != 11:
        raise ValueError('E11 formal library must contain exactly 11 Skills')
    (root / 'frozen/libraries').mkdir(parents=True, exist_ok=True)
    (root / 'frozen/libraries/evolved.json').write_text(json.dumps(library, ensure_ascii=False, indent=2) + '\n')
    tasks = json.loads((root / 'frozen/task_table.json').read_text())
    schedule = [{'cell': 'evolved_autonomous', 'task': row['task_name'], 'attempt': attempt}
                for attempt in range(1, 4) for row in tasks]
    write(root / 'frozen/schedule.json', schedule)
    template = json.loads((root / 'frozen/e10_config.json').read_text())
    template.update(job_name='e11_qwen_autonomous_20261017', jobs_dir=str(root / 'formal'), n_attempts=1)
    template['orchestrator']['n_concurrent_trials'] = 1
    template['orchestrator']['retry']['max_retries'] = 0
    template['agents'][0]['import_path'] = 'runtime:E11Terminus'
    template['environment']['import_path'] = 'runtime:E11Environment'
    template['environment']['kwargs'].update(skill_root=str(root / 'frozen/libraries/evolved'), loading_mode='autonomous')
    template['agents'][0]['kwargs'].update(loading_mode='autonomous', skills_dir='/opt/openclaw-skills',
                                            loading_manifest=str(root / 'formal/load_manifest.json'))
    write(root / 'frozen/executor_template.json', template)
    suffix = catalog_suffix(library)
    write(root / 'formal/load_manifest.json', {
        'mode': 'autonomous', 'skill_ids': [row['skill_id'] for row in library],
        'catalog': [{'skill_id': row['skill_id'], 'description': row['description']} for row in library],
        'files': {}, 'user_query_tail': '你可以读取 /opt/openclaw-skills 下的 skill。',
        'initial_suffix': suffix,
    })
    manifest = json.loads((root / 'manifest.json').read_text())
    manifest.update(stage='formal_prepared', formal_slots=267, formal_physical_max=534,
                    formal_library='frozen/libraries/evolved.json', formal_loading_mode='autonomous',
                    formal_schedule='frozen/schedule.json')
    write(root / 'manifest.json', manifest)
    write(root / 'status.json', {'status': 'formal_prepared', 'generation_calls': manifest.get('generation_calls'),
                                 'evolution_calls': manifest.get('evolution_calls'), 'formal_reserved': 0,
                                 'formal_slots': 267})


if __name__ == '__main__':
    main()
