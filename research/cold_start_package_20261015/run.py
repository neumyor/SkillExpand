"""Bounded real ColdStart comparison; frozen inputs, one generation per arm."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare(root, repository):
    if root.exists():
        raise ValueError('Study root already exists; do not overwrite')
    root.mkdir(parents=True)
    source = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/tb21-e1-fix-git-smoke-20261002-v4')
    trial = Path('/data2/liyishan/tb21-e1-qwen-source/fix-git__ZGZujVi')
    task = Path('/data2/liyishan/tbench2-openclaw-min/sources/terminal-bench-2-1-modelbest/tasks/fix-git')
    frozen = root / 'frozen'
    frozen.mkdir()
    for name in ['clusters.json', 'split.json']:
        shutil.copyfile(source / name, frozen / name)
    shutil.copyfile(source / 'discovery/results/0.json', frozen / 'original_experience.json')
    shutil.copyfile(trial / 'agent/trajectory.json', frozen / 'raw_trajectory.json')
    shutil.copyfile(task / 'instruction.md', frozen / 'public_instruction.md')
    shutil.copytree(repository / 'src/skillexpand', root / 'code/skillexpand',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copyfile(repository / 'research/cold_start_package_20261015/protocol.md', root / 'protocol.md')
    shutil.copyfile(Path(__file__), root / 'run.py')
    shutil.copyfile(repository / 'baseline/src/skillexpand/l1/cold_start.py', frozen / 'old_cold_start.py')
    raw = read(frozen / 'raw_trajectory.json')
    observed = []
    for step in raw['steps']:
        if step.get('tool_calls') and (step.get('observation') or {}).get('results'):
            observed.append({key: step[key] for key in ['step_id', 'timestamp', 'tool_calls', 'observation']
                             if key in step})
    context = {'public_instruction': (frozen / 'public_instruction.md').read_text(),
               'raw_action_observation_pairs': observed,
               'source_policy': 'Raw tool observations are evidence. Original card observations include model narration; do not treat narration as fact. This is one historical task, not cross-task support.'}
    exp = read(frozen / 'original_experience.json')
    exp['experience_card']['source_context'] = context
    write(frozen / 'generation_experience.json', exp)
    sys.path.insert(0, str(root / 'code'))
    from skillexpand.l1.protocol import projection
    write(frozen / 'generation_projection.json', projection(exp['experience_card']))
    assert read(frozen / 'generation_projection.json')['source_context'] == context
    url = 'https://raw.githubusercontent.com/anthropics/skills/main/skills/skill-creator/SKILL.md'
    shutil.copyfile(repository / 'claude_skill_creator.md', frozen / 'claude_skill_creator.md')
    write(frozen / 'claude_source.json', {'url': url, 'retrieved_at': time.time(),
        'mutable_upstream_main': True, 'local_snapshot_retained': True})
    settings = {'model': 'DEEPSEEK_up5zdj', 'temperature': 0, 'max_tokens': 65536,
                'enable_thinking': True, 'stream': True}
    write(frozen / 'generation_settings.json', settings)
    write(root / 'manifest.json', {'protocol': 'bounded-cold-start-package-v1',
        'planned_generation_calls': 2, 'generation_cap': 4, 'calls_per_arm': 1,
        'new_strategies': ['evidence-driven-package'], 'executor_cap': 4,
        'context_tokens': 262144, 'provider': 'https://llm-center.modelbest.co/v1',
        'source_cluster': str(source), 'source_trial': str(trial), 'source_task': str(task),
        'codex_requested_model': 'gpt-6.1-sol', 'codex_requested_reasoning': 'high',
        'codex_root_switch_verified': False, 'formal_loading_started': False})
    write(root / 'status.json', {'status': 'prepared', 'generation_calls': 0, 'executor_slots': 0})
    print(json.dumps({'root': str(root), 'raw_pairs': len(observed),
                      'projection_chars': len((frozen / 'generation_projection.json').read_text())}))


def generate(root):
    sys.path[:0] = [str(root / 'code')]
    from omegaconf import OmegaConf
    from skillexpand import schema as S
    from skillexpand.l1 import cold_start as new
    from skillexpand.l1.family_discovery import load_family_plan
    from skillexpand.persistence.artifacts import load_cold_start
    from skillexpand.persistence.store import SkillLibrary
    from skillexpand.runtime.llm_relay import relay_from_env, coalesce_sse
    from skillexpand.skill_package import materialize_skill
    old = load('frozen_old_cold_start', root / 'frozen/old_cold_start.py')
    exp = S.from_dict(S.TaskExperience, read(root / 'frozen/generation_experience.json'))
    clusters = load_family_plan(root / 'frozen/clusters.json', benchmark='terminalbench')
    settings = read(root / 'frozen/generation_settings.json')
    taskfile = root / 'frozen/task_table.json'
    write(taskfile, [{'task_name': 'fix-git', 'instruction': exp.task, 'source': 'frozen existing cluster'}])
    cfg = OmegaConf.create({'benchmark': {'name': 'terminalbench', 'task_file': str(taskfile),
        'task_prefix': '', 'l1': {'adapter': 'skillexpand.l1.adapters:TerminalBenchAdapter'}},
        'agent': {'llm': settings['model']}, 'models': {'cold_start': settings['model']}})
    plan = S.SplitPlan.make({0: 'train'}, 'terminalbench', 42)
    relay = relay_from_env()
    relay.start()

    def arm(name, module):
        path = root / 'generations' / name
        if path.exists():
            raise ValueError('Generation arm already consumed')
        path.mkdir(parents=True)
        calls = 0

        def ask(prompt):
            nonlocal calls
            if calls:
                raise ValueError('One generation already consumed; no regeneration')
            if len(prompt.encode('utf-8')) + settings['max_tokens'] + 1024 > 262144:
                raise ValueError('Full input exceeds conservative context bound; no truncation')
            calls += 1
            write(path / 'reserved.json', {'at': time.time(), 'call': calls, 'no_retry': True})
            payload = {**settings, 'messages': [{'role': 'user', 'content': prompt}]}
            write(path / 'request.json', payload)
            started = time.time()
            with (path / 'response.sse').open('wb') as stream:
                def chunks():
                    for chunk in relay.transport.stream(payload):
                        data = chunk.encode() if isinstance(chunk, str) else chunk
                        stream.write(data)
                        stream.flush()
                        yield data
                response = coalesce_sse(chunks())
            write(path / 'completion.json', response)
            write(path / 'timing.json', {'seconds': time.time() - started})
            return response['choices'][0]['message']['content']

        outcome = {'arm': name, 'status': 'failed', 'calls': 0}
        try:
            cold = module.ColdStart(cfg, plan, path / 'cold_start', ask=ask, card_batch_size=1,
                                    cold_start_workers=1, family_discovery_workers=1)
            new.freeze(cold.root / 'config.json', OmegaConf.to_container(cfg, resolve=True))
            new.freeze(cold.root / 'clusters.json', clusters.to_dict())
            write(cold.root / 'discovery/results/0.json', S.to_dict(exp))
            cold.synthesize([exp], clusters)
            _, _, skills, _ = load_cold_start(cold.root)
            skill = skills[0]
            persisted = SkillLibrary(cold.root / 'skills.jsonl', benchmark='terminalbench').head(skill.family_id)
            if S.to_dict(skill) != S.to_dict(persisted):
                raise ValueError('Library reload mismatch')
            entry = materialize_skill(skill, path / 'package', package=skill.package_mode)
            for relative, content in skill.resources.items():
                resource = entry.parent / relative
                if resource.read_text() != content:
                    raise ValueError('Resource bytes changed')
                if resource.suffix == '.py':
                    compile(content, str(resource), 'exec')
                elif resource.suffix == '.sh':
                    subprocess.run(['bash', '-n', str(resource)], check=True, capture_output=True)
            write(path / 'library.json', [S.to_dict(skill)])
            outcome.update(status='valid', package_mode=skill.package_mode,
                           entrypoint_format=skill.entrypoint_format, resources=list(skill.resources),
                           body_chars=len(skill.body), description=skill.description)
        except Exception as exc:
            outcome.update(error_type=type(exc).__name__, error=str(exc)[-1800:])
        outcome['calls'] = calls
        write(path / 'outcome.json', outcome)
        print(json.dumps(outcome), flush=True)
        return outcome

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda item: arm(*item), [('old', old), ('new', new)]))
        write(root / 'status.json', {'status': 'generation_finished_pending_review',
            'generation_calls': sum(value['calls'] for value in outcomes), 'executor_slots': 0})
    finally:
        relay.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'generate'])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--repository', type=Path)
    args = parser.parse_args()
    prepare(args.root, args.repository) if args.action == 'prepare' else generate(args.root)
