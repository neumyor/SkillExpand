import time
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import openai
from skillexpand.runtime.models.llm import GPTWrapper, request_policy, retry_delay, wait_for_request_slot
from skillexpand.benchmarks.base import environment_call


def test_model_retries_transient_errors_until_success_with_capped_backoff():
    client = Mock(side_effect=[openai.error.Timeout('offline'),
                               openai.error.APIError('gateway'),
                               openai.error.APIConnectionError('tunnel'),
                               SimpleNamespace(content=' recovered ')])
    with patch('skillexpand.runtime.models.llm.ChatOpenAI', return_value=client) as factory, \
            patch('skillexpand.runtime.models.llm.time.sleep') as sleep, \
            patch.dict('os.environ', EXPE_LLM_TIMEOUT_SECONDS='1'):
        wrapper = GPTWrapper('test', 'EMPTY')
        assert wrapper([]) == 'recovered'
        assert client.call_count == 4
        assert sleep.call_args_list == [((1,),), ((2,),), ((4,),)]
        assert factory.call_args.kwargs['max_retries'] == 0
        assert factory.call_args.kwargs['request_timeout'] == 1
        assert factory.call_args.kwargs['streaming'] is True


def test_retry_delay_stays_at_one_minute():
    assert [retry_delay(i) for i in range(8)] == [1, 2, 4, 8, 16, 32, 60, 60]
    assert request_policy()['retry_forever'] is True
    assert request_policy()['retry_backoff_max'] == 60


def test_model_success_not_retried_and_invalid_timeout_rejected():
    client = Mock(return_value=SimpleNamespace(content=' answer '))
    with patch('skillexpand.runtime.models.llm.ChatOpenAI', return_value=client):
        assert GPTWrapper('test', 'EMPTY')([]) == 'answer'
        assert client.call_count == 1
    for value in ('0', '-1', 'nan', 'inf'):
        with patch.dict('os.environ', EXPE_LLM_TIMEOUT_SECONDS=value), pytest.raises(ValueError):
            request_policy()


def test_shared_request_gate_spaces_parallel_calls(tmp_path):
    gate = tmp_path / 'request-gate.state'
    with patch.dict('os.environ', EXPE_LLM_GATE_FILE=str(gate),
                    EXPE_LLM_REQUEST_INTERVAL_SECONDS='0.05'):
        def call():
            wait_for_request_slot()
            return time.monotonic()
        with ThreadPoolExecutor(max_workers=4) as pool:
            moments = sorted(pool.map(lambda _: call(), range(4)))
    assert all(b - a >= 0.04 for a, b in zip(moments, moments[1:]))


def test_environment_deadline_cancels_timer_after_exception():
    @environment_call
    def stalled():
        time.sleep(1)
    with patch.dict('os.environ', EXPE_ENV_TIMEOUT_SECONDS='0.02'):
        with pytest.raises(TimeoutError, match='Environment stalled'):
            stalled()
    import signal
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0


def _stalled_worker(spec):
    time.sleep(30)
    return {}


def test_native_worker_watchdog_terminates_stalled_process():
    import multiprocessing
    from skillexpand.runtime.parallel import run_generic
    before = {p.pid for p in multiprocessing.active_children()}
    with patch.dict('os.environ', EXPE_WORKER_TIMEOUT_SECONDS='0.1'):
        with pytest.raises(TimeoutError, match='No worker completed'):
            run_generic([SimpleNamespace(benchmark='alfworld')], _stalled_worker, workers=1)
    assert {p.pid for p in multiprocessing.active_children()} == before


def test_cleanup_failure_does_not_replace_task_error():
    from skillexpand.runtime.deadline import close_environment
    agent = SimpleNamespace(env=SimpleNamespace(close=Mock(side_effect=TimeoutError('close'))))
    with pytest.warns(RuntimeWarning, match='cleanup failed'):
        close_environment(agent)


def test_alfworld_respects_engine_terminal_failure():
    from skillexpand.benchmarks.alfworld import AlfworldEnv
    env = AlfworldEnv.__new__(AlfworldEnv)
    env.curr_step, env.max_steps = 1, 50
    env.terminated = env.truncated = env.show_admissible_commands = False
    env.last_action = None
    env.alfworld_run = Mock(return_value=('Game ended.', False, True))
    _, reward, terminated, _, _ = env.step('look')
    assert terminated and not reward


def test_alfworld_routing_uses_threads_without_native_processes():
    import os
    from skillexpand.evaluation.routing import RouteSpec
    from skillexpand.runtime import parallel as PL
    specs = [RouteSpec('alfworld', t, (), '') for t in range(2)]
    with patch.object(PL, '_config'), patch.object(PL.multiprocessing, 'get_context',
                                                  side_effect=AssertionError('Native process for routing')):
        results = PL.run_generic(specs, lambda spec: {'task_id': spec.task_id, 'pid': os.getpid()}, workers=256)
    assert {r['task_id'] for r in results} == {0, 1}
    assert all(r['pid'] == os.getpid() for r in results)


def test_usage_recovery_records_abandoned_request_without_inventing_tokens(tmp_path):
    from skillexpand.persistence.usage import PersistentUsage
    from skillexpand.l1.audit import audit_usage
    checkpoint = tmp_path / 'task.json'
    path = checkpoint.with_suffix('.usage.json')
    tracker = PersistentUsage(path)
    tracker.on_llm_start({}, ['pending request'], run_id='interrupted')
    PersistentUsage(path)
    rows = [json.loads(s) for s in path.with_suffix('.requests.jsonl').read_text().splitlines()]
    assert [r['event'] for r in rows] == ['start', 'abandoned']
    report = audit_usage(checkpoint)
    assert report['failed_requests'] == 1 and report['total_tokens'] == 0
    assert not report['tokens_complete'] and report['abandoned_requests'] == 1
    # An interruption is not an integrity failure: its response is never used.
    assert report['audit_complete']


def test_usage_audit_accepts_transient_error_followed_by_retry(tmp_path):
    from skillexpand.l1.audit import audit_usage
    checkpoint = tmp_path / 'task.json'
    usage = checkpoint.with_suffix('.usage.json')
    tokens = {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}
    usage.write_text(json.dumps({
        'started_requests': 2, 'successful_requests': 1,
        'failed_requests': 1, **tokens,
    }))
    usage.with_suffix('.requests.jsonl').write_text('\n'.join([
        json.dumps({'event': 'start', 'run_id': 'failed'}),
        json.dumps({'event': 'error', 'run_id': 'failed',
                    'error_type': 'APIConnectionError'}),
        json.dumps({'event': 'start', 'run_id': 'retried'}),
        json.dumps({'event': 'end', 'run_id': 'retried',
                    'provider': {'token_usage': tokens}}),
    ]) + '\n')
    report = audit_usage(checkpoint)
    assert report['transient_errors'] == 1
    assert report['audit_complete']
    assert not report['tokens_complete']
