"""Failure taxonomy, retry/repair loops and unit boundaries (fault injection)."""
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import openai
import pytest

from skillexpand.reliability import errors as E
from skillexpand.reliability import retry as R
from skillexpand.reliability.policies import REPAIR, RepairPolicy, RetryPolicy, repair_policy
from skillexpand.reliability.units import FailureCollector, failure_record, guard, map_units
# The provider boundary registers the openai translations it relies on.
import skillexpand.runtime.models.llm  # noqa: E402,F401


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(R.time, 'sleep', lambda seconds: None)


# -- taxonomy ------------------------------------------------------------------

@pytest.mark.parametrize('exc,category,retryable,halt', [
    (E.ProviderUnavailable('x'), 'infrastructure', True, 'after_stage'),
    (E.EnvironmentTimeout('x'), 'infrastructure', True, 'after_stage'),
    (E.WorkerLost('x'), 'infrastructure', True, 'after_stage'),
    (E.RepairExhausted('x'), 'response', True, 'after_stage'),
    (E.ProviderRejected('x'), 'provider_rejected', False, 'stage'),
    (E.AuditFailure('x'), 'integrity', False, 'stage'),
    (E.InvalidInput('x'), 'configuration', False, 'stage'),
    (E.RunLocked('x'), 'configuration', False, 'stage'),
    (KeyError('x'), 'bug', False, 'all'),
    (ValueError('x'), 'bug', False, 'all'),
])
def test_every_failure_has_exactly_one_disposition(exc, category, retryable, halt):
    assert E.classify(exc).value == category
    assert E.disposition(exc).retryable is retryable
    assert E.disposition(exc).halt.value == halt


def test_compatibility_bases_are_kept():
    assert isinstance(E.SchemaViolation('x'), ValueError)
    assert isinstance(E.AuditFailure('x'), ValueError)
    assert isinstance(E.RunLocked('x'), RuntimeError)
    assert isinstance(E.EnvironmentTimeout('x'), TimeoutError)
    assert isinstance(E.StageIncomplete('x'), RuntimeError)


def test_registered_translation_extends_the_taxonomy_without_touching_call_sites():
    class GatewayHiccup(Exception):
        pass

    assert E.classify(GatewayHiccup()) is E.Category.BUG
    E.register_translation(GatewayHiccup, E.ProviderUnavailable)
    try:
        translated = E.translate(GatewayHiccup('502'))
        assert isinstance(translated, E.ProviderUnavailable)
        assert isinstance(translated.__cause__, GatewayHiccup)
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise GatewayHiccup('502')
            return 'ok'

        assert R.retry_transient(flaky, RetryPolicy('t', None, (0,))) == 'ok'
        assert len(calls) == 3
    finally:
        E._TRANSLATIONS[:] = [entry for entry in E._TRANSLATIONS if entry[0] is not GatewayHiccup]


def test_classify_follows_explicit_causes():
    try:
        try:
            raise E.ProviderUnavailable('down')
        except E.ProviderUnavailable as exc:
            raise RuntimeError('wrapped') from exc
    except RuntimeError as wrapped:
        assert E.classify(wrapped) is E.Category.INFRASTRUCTURE


# -- transient retry -------------------------------------------------------------

def test_transient_retry_does_not_retry_rejections_or_bugs():
    policy = RetryPolicy('t', None, (0,))
    with pytest.raises(E.ProviderRejected) as rejected:
        R.retry_transient(Mock(side_effect=openai.error.InvalidRequestError('too long', None)), policy)
    assert isinstance(rejected.value.__cause__, openai.error.InvalidRequestError)
    bug = Mock(side_effect=KeyError('field'))
    with pytest.raises(KeyError):
        R.retry_transient(bug, policy)
    assert bug.call_count == 1


def test_provider_congestion_is_retried_until_the_endpoint_answers():
    from skillexpand.runtime.models.llm import GPTWrapper
    client = Mock(side_effect=[openai.error.ServiceUnavailableError('busy'),
                               openai.error.TryAgain('busy'),
                               SimpleNamespace(content='ok')])
    with patch('skillexpand.runtime.models.llm.ChatOpenAI', return_value=client), \
            patch('skillexpand.runtime.models.llm.time.sleep'):
        assert GPTWrapper('m', 'EMPTY')([]) == 'ok'
    client = Mock(side_effect=openai.error.AuthenticationError('bad key'))
    with patch('skillexpand.runtime.models.llm.ChatOpenAI', return_value=client):
        with pytest.raises(E.ProviderRejected):
            GPTWrapper('m', 'EMPTY')([])
    assert client.call_count == 1


# -- repair ------------------------------------------------------------------------

def test_cached_failures_are_replayed_without_spending_the_budget():
    cache = {0: 'bad', 1: 'bad'}
    fresh_calls = []

    def request(attempt, previous):
        if attempt in cache:
            return cache[attempt], False
        fresh_calls.append((attempt, str(previous.error)))
        return ('good' if attempt == 3 else 'bad'), True

    def parse(raw):
        if raw != 'good':
            raise ValueError('not good')
        return raw

    result = R.call_with_repair(RepairPolicy('t', 2), request, parse)
    assert result.value == 'good' and result.attempts == 4
    assert fresh_calls == [(2, 'not good'), (3, 'not good')]


def test_exhausted_repair_is_retryable_and_degrade_is_a_protocol_outcome():
    bad = R.fresh(lambda: 'bad')
    parse = Mock(side_effect=E.SchemaViolation('bad'))
    with pytest.raises(E.RepairExhausted) as exhausted:
        R.call_with_repair(RepairPolicy('t', 3), bad, parse)
    assert exhausted.value.retryable and len(exhausted.value.failures) == 3
    degraded = R.call_with_repair(RepairPolicy('t', 1, exhausted='degrade'), bad, parse)
    assert degraded.degraded and degraded.value is None and degraded.last_failure.raw == 'bad'


def test_request_errors_are_never_treated_as_output_errors():
    request = Mock(side_effect=E.ProviderRejected('refused'))
    with pytest.raises(E.ProviderRejected):
        R.call_with_repair(RepairPolicy('t', 5), R.fresh(request), lambda raw: raw)
    assert request.call_count == 1


def test_every_repair_site_is_registered_and_overridable(monkeypatch):
    assert {'planner.hypotheses', 'reviewer.predicted_val',
            'discovery.assignment', 'selector.route'} <= set(REPAIR)
    monkeypatch.setenv('EXPE_REVIEWER_ATTEMPTS', '5')
    assert repair_policy('reviewer.predicted_val').attempts == 5
    assert repair_policy('planner.hypotheses').attempts == REPAIR['planner.hypotheses'].attempts


# -- unit boundary ----------------------------------------------------------------

def test_collector_gathers_retryable_failures_and_halts_on_others(tmp_path):
    collector = FailureCollector('stage', tmp_path / 'errors')
    collector.record_exception(E.ProviderUnavailable('busy'), unit_id=1)
    collector.record_exception(E.RepairExhausted('bad output'), unit_id=2)
    with pytest.raises(E.StageIncomplete) as incomplete:
        collector.raise_if_incomplete('Stage incomplete')
    assert incomplete.value.retryable and len(incomplete.value.failures) == 2
    stored = json.loads((tmp_path / 'errors' / '1.json').read_text())
    assert stored['schema'] == 'unit-error-v1' and stored['category'] == 'infrastructure'
    assert stored['type'] == 'ProviderUnavailable' and stored['message'] == 'busy'
    with pytest.raises(E.UnitFailed) as halted:
        collector.record_exception(KeyError('missing field'), unit_id=3)
    assert halted.value.category is E.Category.BUG
    assert E.disposition(halted.value).halt is E.Halt.ALL
    with pytest.raises(E.UnitFailed):
        collector.record({'unit_id': 4, 'message': 'worker bypassed guard()'})


def test_guard_never_raises_and_preserves_the_category():
    value, failure = guard(lambda: 1, unit_id='u', stage='s')
    assert (value, failure) == (1, None)
    _, failure = guard(Mock(side_effect=E.EnvironmentTimeout('slow')), unit_id='u', stage='s')
    assert failure['category'] == 'infrastructure' and failure['retryable']


def test_a_halting_unit_cancels_queued_units():
    started = []
    gate = threading.Event()

    def call(unit):
        started.append(unit)
        if unit == 0:
            raise KeyError('bug')
        gate.wait(1)
        return unit

    collector = FailureCollector('stage')
    with pytest.raises(E.UnitFailed):
        map_units(range(50), call, workers=2, collector=collector,
                  on_success=lambda unit, value: None)
    gate.set()
    time.sleep(0.05)
    assert len(started) < 50


def test_thread_pool_cancels_queued_specs_when_a_sink_halts(monkeypatch):
    from skillexpand.runtime import parallel as PL
    monkeypatch.setattr(PL, '_config', lambda benchmark: None)
    started = []

    def worker(spec):
        started.append(spec.task_id)
        time.sleep(0.01)
        return {'task_id': spec.task_id}

    def sink(record):
        raise E.UnitFailed(failure_record(KeyError('bug'), unit_id=record['task_id'], stage='s'))

    specs = [SimpleNamespace(benchmark='searchqa', task_id=i) for i in range(60)]
    with pytest.raises(E.UnitFailed):
        PL.run_generic(specs, worker, workers=2, on_result=sink)
    assert len(started) < 60


# -- call sites --------------------------------------------------------------------

def test_selector_provider_failure_propagates_but_invalid_choice_is_an_outcome():
    from skillexpand import schema as S
    from skillexpand.evaluation.selector import SkillSelector
    skill = S.Skill('searchqa.one', 'one', 0, 'n', 'Lookup', 'b', S.Provenance(rationale='t'))
    host = SimpleNamespace(benchmark_name='searchqa', llm=Mock(side_effect=E.ProviderRejected('no')))
    with pytest.raises(E.ProviderRejected):
        SkillSelector(host).select('q', [skill])
    host = SimpleNamespace(benchmark_name='searchqa', llm=Mock(return_value='SKILL: invented'))
    outcome = SkillSelector(host).select('q', [skill])
    assert not outcome.ok and outcome.reason == 'unparsable'


def test_family_discovery_retries_output_only_and_exhaustion_is_retryable():
    from skillexpand.l1 import family_discovery as D
    llm = Mock(side_effect=E.ProviderRejected('refused'))
    with pytest.raises(E.ProviderRejected):
        D.tag_tasks({1: 'task'}, llm)
    assert llm.call_count == 1
    llm = Mock(return_value='not json')
    with pytest.raises(E.RepairExhausted) as exhausted:
        D.tag_tasks({1: 'task'}, llm)
    assert llm.call_count == REPAIR['discovery.tags'].attempts
    assert exhausted.value.retryable


def test_campaign_supervisor_halts_sibling_jobs_after_a_bug(tmp_path, monkeypatch):
    from skillexpand import campaign as C
    root = tmp_path
    C.save(root / 'manifest.json', {'repo': str(root), 'python': 'python'})
    monkeypatch.setattr(C, 'verify', lambda r: {})
    monkeypatch.setattr(C, 'environment', lambda r: {})
    monkeypatch.setattr(C.time, 'sleep', lambda s: None)
    killed = []
    monkeypatch.setattr(C.os, 'killpg', lambda pid, sig: killed.append(pid))

    class Child:
        def __init__(self, pid, returncode):
            self.pid, self.returncode, self.polls = pid, returncode, 0

        def poll(self):
            self.polls += 1
            if self.polls > 50:
                raise AssertionError('supervisor kept waiting for a sibling after a bug')
            return self.returncode

        def wait(self):
            self.returncode = -15
            return self.returncode

    children = iter([Child(1, 1), Child(2, None)])
    monkeypatch.setattr(C.subprocess, 'Popen', lambda *a, **k: next(children))
    C.save(root / 'preflight' / 'searchqa' / 'status.json',
           {'status': 'needs_attention', 'failure_category': 'bug', 'halt': 'all'})
    assert C.supervise(root, 'preflight') == 1
    status = C.read(root / 'preflight' / 'status.json')
    assert status['halted_by'] == ['searchqa'] and status['status'] == 'needs_attention'
    assert killed == [2]


# -- review regressions --------------------------------------------------------------

def test_api_error_is_classified_by_http_status():
    def api_error(status):
        return openai.error.APIError('body', http_status=status)

    assert E.classify(api_error(503)) is E.Category.INFRASTRUCTURE
    assert E.classify(api_error(429)) is E.Category.INFRASTRUCTURE
    assert E.classify(api_error(None)) is E.Category.INFRASTRUCTURE
    for status in (400, 401, 413):
        assert E.classify(api_error(status)) is E.Category.PROVIDER_REJECTED


def test_l1_audit_accepts_every_retried_provider_error_name():
    from skillexpand.l1.audit import TRANSIENT_PROVIDER_ERRORS
    assert {'TryAgain', 'ServiceUnavailableError', 'APIError', 'Timeout'} <= TRANSIENT_PROVIDER_ERRORS
    assert 'InvalidRequestError' not in TRANSIENT_PROVIDER_ERRORS


def test_artifact_validation_is_integrity_but_model_output_validation_is_retryable():
    from skillexpand.l1 import family_discovery as D
    assert E.classify(D.DiscoveryError('family plan benchmark mismatch')) is E.Category.INTEGRITY
    with pytest.raises(E.RepairExhausted):
        R.call_with_repair(RepairPolicy('t', 2), R.fresh(lambda: '{}'),
                           lambda raw: (_ for _ in ()).throw(D.DiscoveryError('bad assignment')))


def test_lost_environment_pipe_is_infrastructure():
    import skillexpand.benchmarks.base  # noqa: F401 - registers environment translations
    assert E.classify(BrokenPipeError()) is E.Category.INFRASTRUCTURE
    assert E.classify(EOFError()) is E.Category.INFRASTRUCTURE


def _campaign_job(tmp_path, monkeypatch, results):
    from skillexpand import campaign as C
    from dataclasses import replace
    root = tmp_path
    C.save(root / 'manifest.json', {'repo': str(root), 'python': 'python'})
    monkeypatch.setattr(C, 'verify', lambda r: {})
    monkeypatch.setattr(C, 'environment', lambda r: {})
    monkeypatch.setattr(C, 'audit_stage', lambda *a: {})
    real = C.stage_policy
    monkeypatch.setattr(C, 'stage_policy', lambda: replace(real(), delays=(0,)))
    monkeypatch.setattr(C.time, 'sleep', lambda s: None)
    monkeypatch.delenv('EXPE_STAGE_ATTEMPTS', raising=False)
    calls = []

    def popen(cmd, **kwargs):
        stage, attempt = cmd[cmd.index('--stage') + 1], int(cmd[cmd.index('--attempt') + 1])
        calls.append((stage, attempt))
        rc, record = results(stage, attempt)

        def wait():
            if record is not None:
                C.save(root / 'preflight/searchqa/attempts' / f'{stage}-{attempt}.json', record)
            return rc
        return SimpleNamespace(pid=10, wait=wait)

    monkeypatch.setattr(C.subprocess, 'Popen', popen)
    return C, root, calls


def test_response_failures_need_attention_after_consecutive_stage_attempts(tmp_path, monkeypatch):
    from skillexpand.reliability.policies import STAGE_ATTEMPTS_BY_CATEGORY
    failed = {'status': 'failed', 'category': 'response', 'retryable': True, 'halt': 'after_stage'}
    C, root, calls = _campaign_job(tmp_path, monkeypatch, lambda stage, attempt: (1, failed))
    assert C.run_job(root, 'preflight', 'searchqa') == 1
    assert len(calls) == STAGE_ATTEMPTS_BY_CATEGORY['response']
    status = C.read(root / 'preflight/searchqa/status.json')
    assert status['status'] == 'needs_attention' and status['failure_category'] == 'response'


def test_signal_killed_stage_is_retried_but_unrecorded_exit_is_a_bug(tmp_path, monkeypatch):
    complete = {'status': 'complete', 'retryable': False}
    C, root, calls = _campaign_job(tmp_path, monkeypatch, lambda stage, attempt:
                                   (-9, None) if attempt == 1 else (0, complete))
    assert C.run_job(root, 'preflight', 'searchqa') == 0
    assert calls[:2] == [('cold-start', 1), ('cold-start', 2)]
    C, root, calls = _campaign_job(tmp_path / 'b', monkeypatch, lambda stage, attempt: (1, None))
    (tmp_path / 'b').mkdir(exist_ok=True)
    assert C.run_job(root, 'preflight', 'searchqa') == 1
    assert C.read(root / 'preflight/searchqa/status.json')['halt'] == 'all'


def test_halting_stage_exits_without_joining_workers(tmp_path, monkeypatch):
    from skillexpand import campaign as C
    monkeypatch.setattr(C, 'stage_args', lambda *a: [])
    exits = []
    monkeypatch.setattr(C, 'exit_now', exits.append)
    import skillexpand.cli as cli
    monkeypatch.setattr(cli, 'main', Mock(side_effect=KeyError('bug')))
    C.execute_stage(tmp_path, 'preflight', 'searchqa', 'cold-start', 1)
    assert exits == [1]
    monkeypatch.setattr(cli, 'main', Mock(side_effect=E.StageIncomplete('busy')))
    assert C.execute_stage(tmp_path, 'preflight', 'searchqa', 'cold-start', 2) == 1
    assert exits == [1]
