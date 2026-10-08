"""Supervisor control-flow tests without network calls or benchmark execution."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from skillexpand import campaign as C


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    manifest = {'repo': str(tmp_path), 'python': 'python', 'concurrency': C.CONCURRENCY,
                'autonomous_attempts': 4, 'supervised_attempts': 1, 'batch_size': 50,
                'candidate_count': 3, 'single_candidate': False, 'skill_edit_mode': 'rewrite',
                'acceptance_mode': 'predicted', 'predicted_review_scope': 'val',
                'reviewer_update_mode': 'none', 'reviewer_feedback_size': 0,
                'models': {role: 'm' for role in C.ROLES}}
    C.save(tmp_path / 'manifest.json', manifest)
    monkeypatch.setattr(C, 'verify', lambda root: manifest)
    monkeypatch.setattr(C, 'environment', lambda root: {})
    monkeypatch.setattr(C, 'health', lambda root: {})
    monkeypatch.setattr(C, 'audit_stage', lambda *args: {'integrity': 'passed'})
    real_policy = C.stage_policy
    monkeypatch.setattr(C, 'stage_policy', lambda: replace(real_policy(), delays=(0,)))
    monkeypatch.setenv('EXPE_STAGE_ATTEMPTS', '4')
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
                ('--test-workers', 'test_workers'),
            ):
                assert args[args.index(flag) + 1] == str(expected[key])
            if stage.startswith('evolve-'):
                assert args[args.index('--evolve-rounds') + 1] == stage[-1]
            assert args[args.index('--task-file') + 1].endswith(f'{benchmark}-tasks.json')
            assert args[args.index('--predicted-review-scope') + 1] == 'val'


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


def test_stage_failures_are_classified_by_category_not_message(campaign):
    from skillexpand.reliability import errors as E
    summary = C.failure_summary
    assert summary(E.StageIncomplete('L1 interrupted', [{'category': 'infrastructure'}]))['retryable']
    assert summary(E.RepairExhausted('reviewer output budget exhausted'))['retryable']
    for exc in (E.AuditFailure('audit mismatch'), E.ProviderRejected('invalid key'),
                E.InvalidInput('bad split')):
        assert summary(exc)['retryable'] is False and summary(exc)['halt'] == 'stage'
    # A message mentioning a network failure no longer makes a defect retryable.
    for exc in (RuntimeError('Timeout: service unavailable'), ValueError('unknown frozen task')):
        assert summary(exc) == {'category': 'bug', 'retryable': False, 'halt': 'all'}


def test_default_recovery_survives_more_than_four_stage_failures(campaign, monkeypatch):
    monkeypatch.delenv('EXPE_STAGE_ATTEMPTS')
    calls = fake_processes(monkeypatch, campaign, lambda stage, attempt:
        {'status': 'failed', 'retryable': True} if stage == 'cold-start' and attempt < 10
        else {'status': 'complete', 'retryable': False})
    assert C.run_job(campaign, 'preflight', 'searchqa') == 0
    assert calls[:10] == [('cold-start', n) for n in range(1, 11)]
    assert calls[10:] == [('evolve-1', 1), ('evolve-2', 1)]


def test_input_validation_rejects_missing_tasks_and_unknown_roles():
    with pytest.raises(ValueError):
        C.validate_inputs([{}, {}, {}], {'assignment': {'0': 'train', '1': 'test'}})
    with pytest.raises(ValueError):
        C.validate_inputs([{}, {}, {}], {'assignment': {'0': 'train', '1': 'test', '2': 'training'}})


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
    monkeypatch.setenv('EXPE_LLM_BASE_URL', 'https://llm.example.invalid/v1')
    settings = C.configured_runtime()
    assert settings['model'] == 'test-model'
    assert settings['llm_base_url'] == 'https://llm.example.invalid/v1'
    assert settings['python'] == str(venv_python)
    assert settings['alfworld_config'] == str(files['ALFWORLD_CONFIG'])

    C.save(tmp_path / 'manifest.json', dict(settings, repo=str(tmp_path),
        models={role: settings['model'] for role in C.ROLES},
        request_interval_seconds=C.REQUEST_INTERVAL_SECONDS,
        timeouts={'request': 300,
                  'environment': 120, 'worker_progress': 3600}))
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only-secret')
    environment = C.environment(tmp_path)
    assert environment['OPENAI_API_KEY'] == 'test-only-secret'
    assert environment['ALFWORLD_DATA'] == str(directories['ALFWORLD_DATA'])
    assert environment['EXPE_LLM_GATE_FILE'] == str(tmp_path / 'request-gate.state')
    assert environment['EXPE_LLM_REQUEST_INTERVAL_SECONDS'] == str(C.REQUEST_INTERVAL_SECONDS)
    assert 'test-only-secret' not in (tmp_path / 'manifest.json').read_text()


def test_environment_rejects_endpoint_drift(tmp_path, monkeypatch):
    manifest = {
        'llm_base_url': 'https://frozen.example/v1',
        'timeouts': {'request': 300, 'environment': 120,
                     'worker_progress': 3600},
        'models': {}, 'overlay': str(tmp_path),
        'alfworld_data': str(tmp_path), 'alfworld_config': str(tmp_path / 'cfg'),
        'alfworld_bench_src': str(tmp_path), 'python': str(tmp_path / 'python'),
        'request_interval_seconds': C.REQUEST_INTERVAL_SECONDS,
    }
    C.save(tmp_path / 'manifest.json', manifest)
    (tmp_path / 'cfg').touch()
    (tmp_path / 'python').touch()
    monkeypatch.setenv('EXPE_LLM_BASE_URL', 'https://current.example/v1')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only-secret')
    with pytest.raises(ValueError, match='endpoint differs'):
        C.environment(tmp_path)


def test_usage_ledger_audit_requires_terminal_tokenized_requests(tmp_path):
    log = tmp_path / 'usage' / 'planner.requests.jsonl'
    log.parent.mkdir()
    log.write_text('\n'.join([
        json.dumps({'event': 'start', 'run_id': 'r1'}),
        json.dumps({'event': 'end', 'run_id': 'r1',
                    'provider': {'token_usage': {
                        'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}}}),
    ]) + '\n')
    result = C.audit_usage_ledgers(tmp_path)
    assert result['tokens_complete'] is True
    assert result['total_tokens'] == 5
    log.write_text(json.dumps({'event': 'start', 'run_id': 'r2'}) + '\n')
    assert C.audit_usage_ledgers(tmp_path)['tokens_complete'] is False


def test_campaign_requires_local_configuration(monkeypatch):
    for name in ('EXPE_LLM_MODEL', 'EXPE_LLM_BASE_URL', 'ALFWORLD_PYTHON', 'EXPE_CAMPAIGN_OVERLAY',
                 'ALFWORLD_DATA', 'ALFWORLD_CONFIG', 'ALFWORLD_BENCH_SRC'):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match='ALFWORLD_PYTHON'):
        C.configured_runtime()


def test_health_probes_each_distinct_role_model(tmp_path, monkeypatch):
    models = {role: 'base' for role in C.ROLES}
    models['l2_reviewer'] = 'strong'
    C.save(tmp_path / 'manifest.json', {'models': models})
    monkeypatch.setattr(C, 'environment', lambda root: {
        'EXPE_LLM_BASE_URL': 'https://llm.example.invalid/v1',
        'OPENAI_API_KEY': 'test-only-secret',
    })
    seen = []

    class Response:
        def __init__(self, model):
            self.model = model

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({'model': self.model, 'choices': [
                {'message': {'content': 'OK'}}]}).encode()

    def urlopen(request, timeout):
        model = json.loads(request.data)['model']
        seen.append(model)
        return Response(model)

    monkeypatch.setattr(C.urllib.request, 'urlopen', urlopen)
    # health() exports the campaign environment into this process; undo it.
    monkeypatch.setattr(C.os, 'environ', dict(os.environ))
    result = C.health(tmp_path)
    assert seen == ['base', 'strong']
    assert result['models']['l2_reviewer'] == 'strong'
    assert {item['requested_model'] for item in result['probes']} == {'base', 'strong'}


def _runtime(tmp_path, monkeypatch):
    for name, path in (('ALFWORLD_PYTHON', tmp_path / 'python'),
                       ('ALFWORLD_CONFIG', tmp_path / 'textworld.yaml')):
        path.touch()
        monkeypatch.setenv(name, str(path))
    for name, path in (('EXPE_CAMPAIGN_OVERLAY', tmp_path / 'overlay'),
                       ('ALFWORLD_DATA', tmp_path / 'data'),
                       ('ALFWORLD_BENCH_SRC', tmp_path / 'benchmark-src')):
        path.mkdir()
        monkeypatch.setenv(name, str(path))
    monkeypatch.setenv('EXPE_LLM_MODEL', 'test-model')
    monkeypatch.setenv('EXPE_LLM_BASE_URL', 'https://llm.example.invalid/v1')
    inputs = tmp_path / 'inputs'
    inputs.mkdir()
    for benchmark in C.BENCHMARKS:
        (inputs / f'{benchmark}-tasks.json').write_text(json.dumps([{'question': str(i)} for i in range(7)]))
        assignment = {str(i): part for i, part in enumerate(['train'] * 4 + ['val', 'test', 'test'])}
        (inputs / f'{benchmark}-split.json').write_text(json.dumps({'assignment': assignment}))
    return inputs


def test_prepare_freezes_code_and_a_launcher_that_runs_only_the_frozen_copy(tmp_path, monkeypatch):
    inputs = _runtime(tmp_path, monkeypatch)
    root = tmp_path / 'campaign'
    manifest = C.prepare(root, inputs)
    assert (root / 'code/src/skillexpand/campaign.py').is_file()
    assert (root / 'code/run_campaign.py').read_text() == C.FROZEN_LAUNCHER
    assert 'code/run_campaign.py' in manifest['files']
    # A broken package earlier on PYTHONPATH must not shadow the frozen copy.
    shadow = tmp_path / 'shadow' / 'skillexpand'
    shadow.mkdir(parents=True)
    (shadow / '__init__.py').write_text('raise ImportError("live package used")\n')
    env = dict(os.environ, PYTHONPATH=str(shadow.parent))
    output = subprocess.check_output(
        [sys.executable, str(root / 'code/run_campaign.py'), 'check', '--root', str(root)],
        env=env, text=True)
    assert json.loads(output.strip().splitlines()[-1])['verified'] is True


def test_source_git_drift_is_reported_not_fatal(tmp_path, monkeypatch):
    inputs = _runtime(tmp_path, monkeypatch)
    root = tmp_path / 'campaign'
    C.prepare(root, inputs)
    monkeypatch.setattr(C, 'git_identity', lambda repo: {'commit': 'other', 'dirty': True,
                                                         'status_hash': 'x'})
    manifest = C.verify(root)
    assert C.source_drift(manifest)['changed'] is True
    (root / 'code/src/skillexpand/schema.py').write_text('# edited\n')
    with pytest.raises(ValueError, match='Frozen campaign file changed'):
        C.verify(root)


def test_prepare_refuses_to_run_from_a_frozen_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(C, '__file__', str(tmp_path / 'code/src/skillexpand/campaign.py'))
    with pytest.raises(ValueError, match='source checkout'):
        C.source_checkout()


def test_checkout_shim_hands_campaign_actions_to_the_frozen_launcher(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'run_campaign_shim', Path(__file__).resolve().parents[1] / 'scripts/run_campaign.py')
    shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)
    root = tmp_path / 'campaign'
    assert shim.frozen_launcher(['start', '--root', str(root)]) is None
    (root / 'code').mkdir(parents=True)
    (root / 'code/run_campaign.py').write_text(C.FROZEN_LAUNCHER)
    assert shim.frozen_launcher(['start', '--root', str(root), '--mode', 'full']) == \
        root.resolve() / 'code/run_campaign.py'
    assert shim.frozen_launcher(['prepare', '--root', str(root)]) is None
