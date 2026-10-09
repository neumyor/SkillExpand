import time
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import openai
from skillexpand.runtime.models.llm import GPTWrapper, request_policy, wait_for_request_slot
from skillexpand.runtime.deadline import environment_call


@pytest.mark.parametrize('model,expected', [
    ('deepseek-v4-flash-0731', 65536),
    ('openai/deepseek-v4-flash-0731-tencent', 65536),
    ('DEEPSEEK_up5zdj', 65536),
    ('qwen3.6-flash-distill', None),
])
def test_model_default_output_limit(model, expected):
    with patch.dict('os.environ', {}, clear=True), \
            patch('skillexpand.runtime.models.llm.ChatOpenAI') as factory:
        GPTWrapper(model, 'EMPTY', False)
        assert factory.call_args.kwargs.get('max_tokens') == expected


def test_explicit_output_limit_overrides_deepseek_default():
    with patch.dict('os.environ', EXPE_LLM_MAX_TOKENS='32768'), \
            patch('skillexpand.runtime.models.llm.ChatOpenAI') as factory:
        GPTWrapper('DEEPSEEK_up5zdj', 'EMPTY', False)
        assert factory.call_args.kwargs['max_tokens'] == 32768


def test_model_retry_budget_preserves_original_error():
    client = Mock(side_effect=openai.error.Timeout('offline'))
    with patch('skillexpand.runtime.models.llm.ChatOpenAI', return_value=client) as factory, \
            patch('skillexpand.runtime.models.llm.time.sleep') as sleep, \
            patch.dict('os.environ', EXPE_LLM_RETRIES='2', EXPE_LLM_TIMEOUT_SECONDS='1'):
        wrapper = GPTWrapper('test', 'EMPTY', False)
        with pytest.raises(openai.error.Timeout):
            wrapper([])
        assert client.call_count == 3
        assert sleep.call_count == 2
        assert factory.call_args.kwargs['max_retries'] == 0
        assert factory.call_args.kwargs['request_timeout'] == 1
        assert factory.call_args.kwargs['streaming'] is True


def test_model_success_not_retried_and_invalid_timeout_rejected():
    client = Mock(return_value=SimpleNamespace(content=' answer '))
    with patch('skillexpand.runtime.models.llm.ChatOpenAI', return_value=client):
        assert GPTWrapper('test', 'EMPTY', False)([]) == 'answer'
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
    assert not report['tokens_complete']
