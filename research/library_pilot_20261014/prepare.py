"""Prepare an isolated library run from declared original sources before calls."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / 'code'), str(ROOT)]
from skillexpand import schema as S
from skillexpand.evaluation.selector import SkillSelector
from skillexpand.l2.editor import SkillEditor
from types import SimpleNamespace
from runtime import build_config


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def prepare():
    repo = ROOT.parents[1]
    frozen = ROOT / 'frozen'
    if (ROOT / 'manifest.json').exists():
        raise ValueError('Already prepared; no new calls or overwrites')
    seed = read(frozen / 'seed_library.json')
    original = read(repo / 'runs/tb21-e6-final-library-empirical-20261010/library.json')
    expected = [s for s in original if s['skill_id'] in {s['skill_id'] for s in seed}]
    if seed != expected or len(seed) != 3:
        raise ValueError('Seed differs from declared unmodified E6 original library')
    panel = read(frozen / 'panel.json')
    if set(panel['editable_skills']) != {s['skill_id'] for s in seed}:
        raise ValueError('All seed skills must be eligible, no read-only subset')
    if len(panel['development']) != 5 or len(panel['regression']) != 2:
        raise ValueError('Revised five/two panel required')
    source = repo / 'runs/tb21-e10-qwen-bare-262144-20261014/full_config.json'
    template = read(source)
    template = build_config(template, ROOT, ROOT / 'template', panel['development'][0], ROOT / 'template/skills')
    write(frozen / 'executor_template.json', template)
    write(frozen / 'e10_source_config.json', read(source))
    write(frozen / 'selector_settings.json', {'model': 'qwen3.6-flash-distill', 'stream': True,
          'max_tokens': 65536, 'top_p': .95, 'top_k': 20,
          'enable_thinking': True, 'chat_template_kwargs': {'preserve_thinking': True}})
    write(frozen / 'generation_settings.json', {'model': 'DEEPSEEK_up5zdj', 'stream': True,
          'max_tokens': 65536, 'enable_thinking': True})
    e6 = repo / 'runs/tb21-e6-final-library-empirical-20261010'
    historical_tasks = read(e6 / 'tasks.json')
    identities, tasks = set(), []
    for split in ('development', 'regression'):
        for name in panel[split]:
            idx = next(i for i, task in enumerate(historical_tasks) if task['task_name'] == name)
            record = read(e6 / 'slots' / f'{idx:02d}-1' / 'record.json')
            result_path = Path(record['result_path'])
            source_task = Path(read(result_path)['config']['task']['path'])
            if source_task.name != name:
                raise ValueError('Original task path mismatch')
            shutil.copytree(source_task, frozen / 'tasks' / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            public = (source_task / 'instruction.md').read_text()
            tasks.append({'task_name': name, 'instruction': public, 'split': split})
            for attempt in range(1, 4):
                original_record = read(e6 / 'slots' / f'{idx:02d}-{attempt}' / 'record.json')
                snapshot = Path(original_record['result_path']).parent / 'snapshot.json'
                if snapshot.exists():
                    identity = read(snapshot).get('source_sandbox_id')
                    if identity:
                        identities.add(identity)
    prior = repo / 'runs/three-task-input-ablation-20261014'
    identities.update(read(prior / 'frozen/historical_sandbox_ids.json'))
    identities.update(read(prior / 'identity_ledger.json'))
    write(frozen / 'historical_sandbox_ids.json', sorted(identities))
    write(frozen / 'task_table.json', tasks)
    skills = [S.from_dict(S.Skill, raw) for raw in seed]
    selector = SkillSelector(SimpleNamespace(benchmark_name='terminalbench'))
    selector_system = selector.build_prompt(tasks[0]['instruction'], skills)[0].content
    (frozen / 'selector_system.txt').write_text(selector_system)
    editor = SkillEditor(SimpleNamespace(llm=None), SimpleNamespace(version=0))
    (frozen / 'generation_system.txt').write_text(editor.build_package_prompt(skills[0], {})[0].content)
    for name in ('tmp', 'cache', 'cache/uv', 'cache/pip', 'cache/pycache'):
        directory = ROOT / name
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.resolve().is_relative_to(Path('/data2')):
            raise ValueError('Runtime writes must use data2')
    manifest = {'format': 'library-pilot-v3', 'stage': 'library_pilot', 'remote_root': str(ROOT),
                'protocol_revision': 3, 'protocol_revised_before_launch': True,
                'executor_context': 262144, 'output_tokens': 65536,
                'editable_skills': panel['editable_skills'], 'workers': 21,
                'executor_max': 66, 'executor_planned': 62, 'development_max': 24,
                'development_planned': 20, 'generation_max': 6,
                'codex_requested_model': 'gpt-6.1-sol', 'codex_requested_reasoning': 'high',
                'codex_root_switch_verified': False, 'evolution_truncation': False,
                'memory_projection_used': False, 'seed_source': str(e6 / 'library.json'),
                'e10_settings_source': str(source), 'native_source_commit': '34284c36470d4b99e5a9bd8e5a65d52a8dc8a01c',
                'harbor_python': '/data2/liyishan/tbench2-openclaw-min/.venv/bin/python',
                'provider': 'https://llm-center.modelbest.co/v1', 'final_feedback_used': False,
                'regression_feedback_used': False, 'prior_protocol_started': False}
    write(ROOT / 'manifest.json', manifest)
    files = [p for p in ROOT.glob('*.py')]
    files += [ROOT / 'protocol.md', ROOT / 'manifest.json']
    files += [p for p in (ROOT / 'code').rglob('*') if p.is_file() and p.suffix == '.py']
    files += [p for p in frozen.rglob('*') if p.is_file() and '__pycache__' not in p.parts]
    bindings = []
    for index, path in enumerate(sorted(set(files))):
        snapshot = ROOT / 'binding_snapshots' / f'{index:04d}.bin'
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_bytes(path.read_bytes())
        bindings.append({'current': str(path.relative_to(ROOT)), 'snapshot': str(snapshot.relative_to(ROOT))})
    write(ROOT / 'bindings.json', bindings)
    write(ROOT / 'preaudit.json', {'passed': True, 'bindings': len(bindings),
          'original_seed_verified': True, 'old_protocol_started': False,
          'context_all_phases': 262144, 'untruncated_evolution': True,
          'no_model_calls': True, 'no_sandbox_calls': True})
    print(json.dumps({'prepared': str(ROOT), 'bindings': len(bindings), 'planned_executor': 62}))


if __name__ == '__main__':
    prepare()
