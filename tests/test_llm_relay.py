import json
import threading
import time
import urllib.request
from urllib.error import HTTPError

import pytest

from skillexpand.runtime.llm_relay import (
    RelayError,
    TencentSandboxLLMRelay,
    classify_provider_error,
    command_provider_error,
    coalesce_sse,
    iter_sse_events,
)


class FakeTransport:
    sandbox_id = "sandbox-test"

    def __init__(self):
        self.payloads = []
        self.closed = False

    def stream(self, payload):
        self.payloads.append(payload)
        yield b'data: {"id":"chatcmpl-test","choices":[{"delta":{"role":"assistant","content":"OK"}}]}\n\n'
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        yield b'data: [DONE]\n\n'

    def close(self):
        self.closed = True


def test_relay_loopback_chat_and_models():
    transport = FakeTransport()
    relay = TencentSandboxLLMRelay(transport)
    base = relay.start()
    try:
        import urllib.request

        models = json.loads(urllib.request.urlopen(base + "/models").read())
        assert models["object"] == "list"
        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps({"model": "qwen3.6-flash-distill", "messages": [], "stream": True}).encode(),
            headers={"Content-Type": "application/json"},
        )
        body = urllib.request.urlopen(req).read().decode()
        events = list(iter_sse_events([body]))
        assert events[0]["choices"][0]["delta"]["content"] == "OK"
        assert transport.payloads[0]["model"] == "qwen3.6-flash-distill"
        assert transport.payloads[0].get("stream") is True
    finally:
        relay.close()
    assert transport.closed


def test_provider_error_classification():
    assert classify_provider_error(TimeoutError()) == "provider_timeout"
    assert classify_provider_error(ConnectionError()) == "provider_connectivity"
    error = HTTPError("https://example.invalid", 429, "busy", {}, None)
    assert classify_provider_error(error) == "provider_429"
    error = HTTPError("https://example.invalid", 503, "down", {}, None)
    assert classify_provider_error(error) == "provider_http_error"


@pytest.mark.parametrize("status", [400, 429, 503])
def test_command_provider_status_ignores_traceback_and_request_id(status):
    body = 'response_format unsupported; request_id=abc4296; urlopen(req, timeout=300)'
    stderr = 'RELAY_PROVIDER_ERROR ' + json.dumps({'status': status, 'body': body})
    error = command_provider_error(stderr)
    assert error.status == status
    assert error.error_class == ('provider_429' if status == 429 else 'provider_http_error')
    assert body in str(error)
    assert command_provider_error(body) is None


def test_command_provider_explicit_timeout():
    error = command_provider_error('RELAY_PROVIDER_ERROR {"type":"provider_timeout"}')
    assert error.error_class == 'provider_timeout'


@pytest.mark.parametrize("status", [400, 429, 503])
def test_relay_preserves_provider_http_status(status):
    class ErrorTransport(FakeTransport):
        def stream(self, payload):
            raise RelayError('unsupported response_format', 'provider_http_error', status)
            yield

    relay = TencentSandboxLLMRelay(ErrorTransport())
    base = relay.start()
    try:
        request = urllib.request.Request(base + '/chat/completions',
            data=b'{"stream":true}', headers={'Content-Type': 'application/json'})
        with pytest.raises(HTTPError) as caught:
            urllib.request.urlopen(request)
        assert caught.value.code == status
        assert json.loads(caught.value.read())['error']['message'] == 'unsupported response_format'
    finally:
        relay.close()


def test_reasoning_only_stream_is_an_explicit_error_not_empty_success():
    class ReasoningTransport(FakeTransport):
        def stream(self, payload):
            yield b'data: {"choices":[{"delta":{"reasoning_content":"unfinished reasoning"}}]}\n\n'
            yield b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'
            yield b'data: [DONE]\n\n'
    relay = TencentSandboxLLMRelay(ReasoningTransport())
    base = relay.start()
    try:
        request = urllib.request.Request(base + '/chat/completions',
            data=b'{"stream":true}', headers={'Content-Type': 'application/json'})
        body = urllib.request.urlopen(request).read().decode()
        assert 'provider_output_truncated' in body
        assert '[DONE]' not in body
        with pytest.raises(RelayError, match='no visible answer'):
            list(iter_sse_events([body]))
    finally:
        relay.close()


def test_sse_parser_handles_chunk_boundaries_and_done():
    chunks = [
        b'data: {"choices":[{"delta":{"content":"O',
        b'K"}}]}\n\n',
        b'data: [DONE]\n\n',
    ]
    events = list(iter_sse_events(chunks))
    assert events[0]["choices"][0]["delta"]["content"] == "OK"
    assert coalesce_sse(chunks)["choices"][0]["message"]["content"] == "OK"


def test_sse_reasoning_content_is_used_when_content_channel_is_empty():
    chunks = [
        b'data: {"choices":[{"delta":{"role":"assistant","reasoning_content":"hel',
        b'lo"}}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
        b'data: [DONE]\n\n',
    ]
    assert coalesce_sse(chunks)["choices"][0]["message"]["content"] == "hello"


def test_sse_visible_content_wins_over_reasoning_fallback():
    chunks = [
        b'data: {"choices":[{"delta":{"reasoning_content":"internal"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"answer"}}]}\n\n',
        b'data: [DONE]\n\n',
    ]
    assert coalesce_sse(chunks)["choices"][0]["message"]["content"] == "answer"


def test_relay_error_does_not_expose_payload():
    error = RelayError("Tencent relay request failed: provider_connectivity", "provider_connectivity")
    assert "OPENAI_API_KEY" not in str(error)
    assert "prompt" not in str(error)


def test_loopback_requests_overlap_without_global_serialization():
    class OverlapTransport(FakeTransport):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.max_active = 0
            self.guard = threading.Lock()

        def stream(self, payload):
            with self.guard:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                yield b'data: {"choices":[{"delta":{"role":"assistant","content":"OK"}}]}\n\n'
                time.sleep(0.05)
                yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                yield b'data: [DONE]\n\n'
            finally:
                with self.guard:
                    self.active -= 1

    transport = OverlapTransport()
    relay = TencentSandboxLLMRelay(transport)
    base = relay.start()
    errors = []

    def call():
        try:
            req = urllib.request.Request(
                base + "/chat/completions",
                data=json.dumps({"model": "test", "messages": [], "stream": True}).encode(),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req).read()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        assert not errors
        assert all(not thread.is_alive() for thread in threads)
        assert transport.max_active == 2
    finally:
        relay.close()
