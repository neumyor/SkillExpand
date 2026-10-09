"""Regression checks for installed resources, historical inputs and process workers."""
import os
import subprocess
import sys
from pathlib import Path

from skillexpand.persistence.io import code_signature
from skillexpand.runtime import parallel


def _spawn_probe(value):
    from skillexpand.runtime.agent_factory import load_config
    from skillexpand.persistence.io import code_signature
    return {'value': value, 'benchmark': load_config('alfworld').benchmark.name,
            'environment_tracked': 'benchmarks/alfworld.py' in code_signature()}


def test_cli_and_packaged_configs_work_outside_checkout(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(repo / 'src'))
    result = subprocess.run([sys.executable, '-m', 'skillexpand', '--help'], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    result = subprocess.run([sys.executable, '-c',
        "from skillexpand.runtime.agent_factory import load_config; "
        "print(load_config('searchqa').benchmark.l1.adapter)"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'skillexpand.l1.adapters:SearchQAAdapter' in result.stdout


def test_fingerprint_covers_environment_prompts_and_all_stages():
    signature = code_signature()
    assert {'benchmarks/alfworld.py', 'benchmarks/searchqa.py', 'l1/runner.py',
            'l2/update.py', 'runtime/prompts/alfworld.py',
            'runtime/models/llm.py', 'evaluation/validation.py'} <= signature.keys()
    assert signature == code_signature()


def test_spawned_workers_resolve_installed_module_paths():
    results = parallel.run_generic([1, 2], _spawn_probe, workers=2)
    assert sorted(r['value'] for r in results) == [1, 2]
    assert all(r['benchmark'] == 'alfworld' and r['environment_tracked'] for r in results)


def test_data_preparation_is_repeatable_and_rejects_conflicts(tmp_path):
    import io
    import tarfile
    import pytest
    from scripts.prepare_data import unpack
    archive = tmp_path / 'data.tar'
    with tarfile.open(archive, 'w') as bundle:
        content = b'example domain'
        member = tarfile.TarInfo('ExpeL-sha/data/alfworld/logic/alfred.pddl')
        member.size = len(content)
        bundle.addfile(member, io.BytesIO(content))
    target = tmp_path / 'assets'
    assert unpack(archive, target) == 1
    assert unpack(archive, target) == 1
    (target / 'logic/alfred.pddl').write_text('changed')
    with pytest.raises(ValueError, match='Existing asset differs'):
        unpack(archive, target)


def test_empty_alfworld_observation_is_preserved():
    from skillexpand.benchmarks.alfworld import AlfworldEnv
    from unittest.mock import Mock
    # Exercise the actual wrapper without importing or starting the optional simulator.
    env = AlfworldEnv.__new__(AlfworldEnv)
    env.last_action = ''
    env.curr_step = 1
    env.max_steps = 20
    env.terminated = False
    env.truncated = False
    env._admissible_commands = []
    env.show_admissible_commands = False
    env.is_success = False
    observation = 'The cabinet 1 is open. In it, you see nothing.'
    env.alfworld_run = Mock(return_value=(observation, False, False))
    result = env.step('look')
    assert result[0] == observation
    assert result[1] is False
