"""Freeze the closed E10 panel and E11 budgets before any model call."""
import argparse
import json
from pathlib import Path
import shutil


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def prepare(root, repository):
    remote = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand')
    reg = remote / 'runs/tb21-e10-qwen-bare-262144-20261014'
    revision = reg / 'rerun_rpcfix_full_20261016'
    amendment = read(revision / 'tmux_missing_scoring_amendment_20261017.json')
    source = Path(amendment['source_job'])
    results = sorted(source.glob('*/result.json'))
    if root.exists() or len(results) != 267:
        raise ValueError('Existing E11 root or incomplete E10 panel')
    if any(remote.joinpath('runs').glob('*e11*')):
        raise ValueError('Existing E11 registration; inspect instead of duplicate launch')
    root.mkdir(parents=True)
    frozen = root / 'frozen'
    shutil.copytree(reg, frozen / 'e10_registration', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    retries = source.parent / 'e10_rpcfix_full_retry_archives'
    if retries.exists():
        write(frozen / 'e10_retry_archive_manifest.json', {
            'source': str(retries),
            'preserved_in_place': True,
            'entries': sorted(str(p.relative_to(retries)) for p in retries.rglob('*') if p.is_file()),
        })
    shutil.copytree(repository / 'src/skillexpand', root / 'code/skillexpand',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    source_code = repository / 'research/e11_20261017'
    for path in source_code.iterdir():
        if path.suffix in ('.py', '.md'):
            shutil.copyfile(path, root / path.name)
    shutil.copyfile(repository / 'research/library_pilot_20261014/input_filter.py', root / 'input_filter.py')
    runtime = frozen / 'runtime'
    shutil.copytree('/data2/liyishan/tb21-tencent-skill/python', runtime / 'tencent/python',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copytree('/data2/liyishan/tbench2-openclaw-min/.venv/lib/python3.12/site-packages/harbor',
                    runtime / 'framework/harbor', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copyfile(remote / 'scripts/e10_bare_environment.py', runtime / 'tencent/e10_bare_environment.py')
    shutil.copyfile(remote / 'scripts/e10_bare_environment.py', root / 'e10_bare_environment.py')
    for name in ('runner.py', 'study.py', 'watch_files.py', 'evidence.py'):
        shutil.copyfile(repository / 'research/loading_factorial_20261015' / name, root / name)
    config = read(revision / 'config.json')
    shutil.copytree(config['datasets'][0]['path'], frozen / 'tasks',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    scopes = [{k: row[k] for k in ('skill_id', 'family_id', 'name', 'description')}
              for row in read(remote / 'runs/skill-read-prompt-r2-20261015/frozen/library.json')]
    assert len(scopes) == 11
    write(frozen / 'scopes.json', scopes)
    shutil.copyfile(remote / 'runs/skill-read-prompt-r2-20261015/frozen/library.json', frozen / 'seed_library.json')
    write(frozen / 'e10_config.json', config)
    tasks = []
    for task_path in sorted((frozen / 'tasks').iterdir()):
        instruction = (task_path / 'instruction.md').read_text()
        tasks.append({'task_name': task_path.name, 'instruction': instruction, 'source': 'E10 frozen task snapshot'})
    if len(tasks) != 89:
        raise ValueError(f'Expected 89 task snapshots, found {len(tasks)}')
    write(frozen / 'task_table.json', tasks)
    write(frozen / 'e10_amendment.json', amendment)
    write(frozen / 'e10_result_index.json', [str(p.relative_to(source)) for p in results])
    trial_rows = []
    derived = {row['trial']: row['amended_reward'] for row in amendment['tmux_trials']}
    rollouts = frozen / 'e10_rollouts'
    for result in results:
        value = read(result)
        trial = result.parent.name
        exception = value.get('exception_info') or {}
        official = (value.get('verifier_result') or {}).get('rewards', {}).get('reward')
        local_result = rollouts / trial / 'result.json'
        local_trajectory = rollouts / trial / 'agent/trajectory.json'
        local_result.parent.mkdir(parents=True, exist_ok=True)
        local_trajectory.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(result, local_result)
        shutil.copyfile(result.parent / 'agent/trajectory.json', local_trajectory)
        trial_rows.append({
            'task_name': value['task_name'], 'trial': trial,
            'result_path': str(local_result),
            'trajectory_path': str(local_trajectory),
            'official_reward': official, 'derived_reward': derived.get(trial),
            'exception_type': exception.get('exception_type'),
        })
    if len(trial_rows) != 267:
        raise ValueError('E10 trial index is not closed')
    write(frozen / 'e10_trials.json', sorted(trial_rows, key=lambda row: (row['task_name'], row['trial'])))
    write(frozen / 'e10_closed_set.json', {
        'expected': amendment['after_user_policy']['expected'],
        'official_reward_1': amendment['after_user_policy']['reward_1'],
        'official_reward_0': amendment['after_user_policy']['reward_0'],
        'derived_reward_0': len(amendment['tmux_trials']), 'missing': 0,
        'original_evidence_preserved': True,
    })
    write(root / 'manifest.json', {
        'protocol': 'e11-frozen-qwen-library-v1', 'stage': 'prepared',
        'task_count': 89, 'attempts_per_task': 3, 'formal_slots': 267,
        'executor_model': 'qwen3.6-flash-distill', 'method_model': 'DEEPSEEK_up5zdj',
        'context_tokens': 262144, 'output_tokens': 65536,
        'provider': 'https://llm-center.modelbest.co/v1', 'top_p': 0.95, 'top_k': 20,
        'enable_thinking': True, 'preserve_thinking': True, 'interleaved_thinking': True,
        'enable_summarize': True, 'proactive_summarization_threshold': 8000,
        'concurrency': 100, 'agent_timeout_seconds': 7200, 'harbor_max_retries': 1,
        'initial_package_source': 'frozen/seed_library.json',
        'initial_synthesis_calls': 0, 'generation_passes': 0,
        'evolution_passes': 1, 'qwen_development_slots': 0,
        'skill_count': 11, 'user_query_tail': '你可以读取 /opt/openclaw-skills 下的 skill。',
        'external_selector': False, 'automatic_reader': False, 'body_injection': False,
        'mandatory_read_gate': False, 'e10_results_returned_to_generation': False,
        'e11_final_results_returned_to_generation': False,
        'e10_source_job': amendment['source_job'], 'e10_revision': str(revision),
        'e10_formal_untouched': True, 'no_old_experiment_rerun': True,
        'raw_e10_rollouts_preserved_at': str(source),
        'raw_e10_retry_archives_preserved_at': str(retries),
    })
    environment = {
        'OPENAI_API_KEY': '', 'TB21_METHOD_API_KEY': '', 'TB21_EXECUTOR_API_KEY': '',
        'E2B_API_KEY': '', 'E2B_DOMAIN': 'ap-beijing.tencentags.com',
        'E2B_VALIDATE_API_KEY': 'false', 'TBENCH_TENCENT_ENV_FILE': '/dev/null',
        'TBENCH_PERSIST_SANDBOXES': '0', 'TBENCH_E2B_RELAY_TEMPLATE': 'sdt-28lb4d9k',
        'TBENCH_E2B_RELAY_USE_TEMPLATE_DEFAULT': '1',
        'TBENCH_RELAY_PROVIDER_BASE': 'https://llm-center.modelbest.co/v1',
        'EXPE_LLM_TIMEOUT_SECONDS': '1800', 'EXPE_LLM_RETRIES': '0',
        'TBENCH_TOTAL_WORKER_LIMIT': '250', 'TBENCH_E2B_START_CONCURRENCY': '4',
        'TBENCH_E2B_RPC_CONCURRENCY': '12',
    }
    write(root / 'frozen/environment.json', environment)
    write(root / 'status.json', {'status': 'prepared', 'generation_calls': 0,
                                 'evolution_calls': 0, 'formal_reserved': 0})
    print(json.dumps({'prepared': str(root), 'tasks': len(tasks), 'formal_slots': 267,
                      'e10_results': len(results), 'skills': len(scopes)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--repository', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.root, args.repository)
