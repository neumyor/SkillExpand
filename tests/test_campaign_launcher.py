"""Supervisor control-flow tests without network calls or benchmark execution."""
import importlib.util
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('campaign_launcher',
    Path(__file__).resolve().parents[1] / 'scripts/run_campaign.py')
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    manifest = {'repo': str(tmp_path), 'python': 'python', 'concurrency': C.CONCURRENCY,
                'autonomous_attempts': 4, 'batch_size': 50, 'candidate_count': 3}
    C.save(tmp_path / 'manifest.json', manifest)
    monkeypatch.setattr(C, 'verify', lambda root: manifest)
    monkeypatch.setattr(C, 'environment', lambda root: {})
    monkeypatch.setattr(C, 'health', lambda root: {})
    monkeypatch.setattr(C, 'audit_stage', lambda *args: {'integrity': 'passed'})
    monkeypatch.setattr(C, 'RETRY_DELAYS', (0, 0, 0))
    return tmp_path


def fake_processes(monkeypatch, root, fail):
    calls = []
    def popen(cmd, **kwargs):
        stage, attempt = cmd[cmd.index('--stage') + 1], int(cmd[cmd.index('--attempt') + 1])
        calls.append((stage, attempt))
        result = fail(stage, attempt)
        def wait():
            C.save(root / 'preflight/searchqa/attempts' / f'{stage}-{attempt}.json', result)
            return 0 if result['status'] == 'complete' else 1
        return SimpleNamespace(pid=100 + len(calls), wait=wait)
    monkeypatch.setattr(C.subprocess, 'Popen', popen)
    return calls


def test_order_retries_and_resume_do_not_repeat_completed_stages(campaign, monkeypatch):
    calls = fake_processes(monkeypatch, campaign, lambda stage, attempt:
        {'status': 'failed', 'retryable': True} if stage == 'cold-start' and attempt == 1
        else {'status': 'complete', 'retryable': False})
    assert C.run_job(campaign, 'preflight', 'searchqa') == 0
    assert calls == [('cold-start', 1), ('cold-start', 2), ('evolve-1', 1), ('evolve-2', 1)]
    assert C.run_job(campaign, 'preflight', 'searchqa') == 0
    assert len(calls) == 4


@pytest.mark.parametrize('retryable,count', [(False, 1), (True, 4)])
def test_failure_stops_following_stages_and_bounds_retries(campaign, monkeypatch, retryable, count):
    calls = fake_processes(monkeypatch, campaign, lambda *args: {'status': 'failed', 'retryable': retryable})
    assert C.run_job(campaign, 'preflight', 'searchqa') == 1
    assert calls == [('cold-start', n) for n in range(1, count + 1)]
    assert C.read(campaign / 'preflight/searchqa/status.json')['status'] == 'needs_attention'


def test_explicit_stage_arguments_and_concurrency(campaign):
    for benchmark, expected in C.CONCURRENCY.items():
        for stage in C.STAGES:
            args = C.stage_args(campaign, 'full', benchmark, stage)
            assert '--resume' in args
            for flag, key in (
                ('--cold-start-workers', 'cold_start_workers'),
                ('--family-discovery-workers', 'family_discovery_workers'),
                ('--evolve-l1-workers', 'evolve_l1_workers'),
                ('--l2-review-workers', 'l2_review_workers'),
                ('--final-workers', 'final_workers'),
            ):
                assert args[args.index(flag) + 1] == str(expected[key])
            if stage.startswith('evolve-'):
                assert args[args.index('--evolve-rounds') + 1] == stage[-1]
            assert args[args.index('--task-file') + 1].endswith(f'{benchmark}-tasks.json')


def test_full_start_requires_matching_preflight(campaign):
    with pytest.raises(FileNotFoundError):
        C.start(campaign, 'full')
    C.save(campaign / 'preflight/complete.json', {'status': 'complete', 'manifest_hash': 'wrong'})
    with pytest.raises(ValueError, match='Matching complete preflight'):
        C.start(campaign, 'full')


def test_detached_launch_and_duplicate_pid_refusal(campaign, monkeypatch):
    invocations = []
    def popen(cmd, **kwargs):
        invocations.append(kwargs)
        return SimpleNamespace(pid=12345)
    monkeypatch.setattr(C.subprocess, 'Popen', popen)
    assert C.start(campaign, 'preflight')['pid'] == 12345
    assert invocations[0]['start_new_session'] is True
    assert invocations[0]['stdin'] == C.subprocess.DEVNULL
    monkeypatch.setattr(C.os, 'kill', lambda *args: None)
    with pytest.raises(ValueError, match='duplicate launch'):
        C.start(campaign, 'preflight')
    assert len(invocations) == 1


def test_validation_errors_do_not_retry_due_to_old_network_error(campaign):
    C.save(campaign / 'run/discovery/errors/0.json', {'error': 'Timeout: service unavailable'})
    assert not C.retryable_failure(ValueError('audit mismatch'), campaign / 'run', 0)
    assert C.retryable_failure(RuntimeError('L1 interrupted'), campaign / 'run', 0)
    assert not C.retryable_failure(RuntimeError('L1 interrupted'), campaign / 'run', time.time() + 10)


def test_input_validation_rejects_missing_tasks_and_unknown_roles():
    with pytest.raises(ValueError):
        C.validate_inputs([{}, {}, {}], {'assignment': {'0': 'source', '1': 'final'}})
    with pytest.raises(ValueError):
        C.validate_inputs([{}, {}, {}], {'assignment': {'0': 'source', '1': 'final', '2': 'training'}})


def test_campaign_runtime_uses_local_configuration(tmp_path, monkeypatch):
    files = {
        'ALFWORLD_PYTHON': tmp_path / 'python',
        'ALFWORLD_CONFIG': tmp_path / 'textworld.yaml',
    }
    directories = {
        'EXPE_CAMPAIGN_OVERLAY': tmp_path / 'overlay',
        'ALFWORLD_DATA': tmp_path / 'data',
        'ALFWORLD_BENCH_SRC': tmp_path / 'benchmark-src',
    }
    for name, path in files.items():
        path.touch()
        monkeypatch.setenv(name, str(path))
    venv_python = tmp_path / 'venv-python'
    venv_python.symlink_to(files['ALFWORLD_PYTHON'])
    monkeypatch.setenv('ALFWORLD_PYTHON', str(venv_python))
    for name, path in directories.items():
        path.mkdir()
        monkeypatch.setenv(name, str(path))
    monkeypatch.setenv('EXPE_LLM_MODEL', 'test-model')
    settings = C.configured_runtime()
    assert settings['model'] == 'test-model'
    assert settings['python'] == str(venv_python)
    assert settings['alfworld_config'] == str(files['ALFWORLD_CONFIG'])

    C.save(tmp_path / 'manifest.json', dict(settings, repo=str(tmp_path),
        request_interval_seconds=C.REQUEST_INTERVAL_SECONDS,
        timeouts={'request': 300, 'request_retries': 2,
                  'environment': 120, 'worker_progress': 3600}))
    monkeypatch.setenv('EXPE_LLM_BASE_URL', 'https://llm.example.invalid/v1')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only-secret')
    environment = C.environment(tmp_path)
    assert environment['OPENAI_API_KEY'] == 'test-only-secret'
    assert environment['ALFWORLD_DATA'] == str(directories['ALFWORLD_DATA'])
    assert environment['EXPE_LLM_GATE_FILE'] == str(tmp_path / 'request-gate.state')
    assert environment['EXPE_LLM_REQUEST_INTERVAL_SECONDS'] == str(C.REQUEST_INTERVAL_SECONDS)
    assert 'test-only-secret' not in (tmp_path / 'manifest.json').read_text()


def test_campaign_requires_local_configuration(monkeypatch):
    for name in ('EXPE_LLM_MODEL', 'ALFWORLD_PYTHON', 'EXPE_CAMPAIGN_OVERLAY',
                 'ALFWORLD_DATA', 'ALFWORLD_CONFIG', 'ALFWORLD_BENCH_SRC'):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match='ALFWORLD_PYTHON'):
        C.configured_runtime()
