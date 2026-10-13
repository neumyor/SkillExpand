"""Tencent E2B backed OpenAI-compatible LLM relay.

The relay binds only to loopback.  The provider request is executed inside a
persistent E2B sandbox so jinan40 never needs direct network access to the
Tencent ModelBest endpoint.
"""
from __future__ import annotations

import asyncio
import codecs
import json
import logging
import os
import shlex
import threading
import time
import uuid
from queue import Empty, Queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LOG = logging.getLogger(__name__)


def default_output_token_limit(model: str) -> int | None:
    return 65536 if str(model).rsplit('/', 1)[-1].lower().startswith('deepseek') else None


_RELAY_SCRIPT = r'''import json, os, sys, urllib.request, urllib.error, socket
path = sys.argv[1]
with open(path, "rb") as stream:
    payload = stream.read()
try:
    request = json.loads(payload)
except Exception:
    request = {}
request["stream"] = True
payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
req = urllib.request.Request(os.environ["MODEL_API_BASE"].rstrip("/") + "/chat/completions", data=payload,
    headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"], "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=int(os.environ.get("RELAY_PROVIDER_TIMEOUT", "120"))) as response:
        if 'text/event-stream' not in response.headers.get('Content-Type', ''):
            raise RuntimeError('provider did not return SSE')
        for line in response:
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
except urllib.error.HTTPError as exc:
    sys.stderr.write("RELAY_PROVIDER_ERROR " + json.dumps({"status": exc.code,
        "body": exc.read().decode("utf-8", "replace")[:8000]}) + "\n")
    sys.exit(1)
except (TimeoutError, socket.timeout) as exc:
    sys.stderr.write('RELAY_PROVIDER_ERROR ' + json.dumps({"type": "provider_timeout"}) + '\n')
    sys.exit(1)
except urllib.error.URLError as exc:
    kind = "provider_timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "provider_connectivity"
    sys.stderr.write('RELAY_PROVIDER_ERROR ' + json.dumps({"type": kind}) + '\n')
    sys.exit(1)
'''


def classify_provider_error(exc: BaseException) -> str:
    if isinstance(exc, HTTPError):
        if exc.code == 429:
            return "provider_429"
        if exc.code >= 500:
            return "provider_http_error"
        return "provider_http_error"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "provider_timeout"
    if isinstance(exc, (URLError, ConnectionError, OSError)):
        return "provider_connectivity"
    return "provider_error"


class RelayError(RuntimeError):
    def __init__(self, message: str, error_class: str = "provider_error", status: int | None = None):
        super().__init__(message)
        self.error_class = error_class
        self.status = status or (429 if error_class == "provider_429" else 503)


def command_provider_error(stderr: str) -> RelayError | None:
    """Read the sandbox's explicit error record, never traceback substrings."""
    for line in stderr.splitlines():
        if not line.startswith("RELAY_PROVIDER_ERROR "):
            continue
        try:
            record = json.loads(line[len("RELAY_PROVIDER_ERROR "):])
        except json.JSONDecodeError:
            continue
        status = record.get("status")
        if isinstance(status, int) and 400 <= status <= 599:
            kind = "provider_429" if status == 429 else "provider_http_error"
            return RelayError(f"Tencent provider HTTP {status}: {record.get('body', '')}", kind, status)
        if record.get("type") in {"provider_timeout", "provider_connectivity"}:
            return RelayError("Tencent provider request failed: " + record["type"], record["type"])
    return None


def iter_sse_events(chunks):
    """Yield decoded SSE data objects while tolerating arbitrary chunk boundaries."""
    pending = ""
    decoder = codecs.getincrementaldecoder("utf-8")()
    data_lines = []
    for chunk in chunks:
        pending += decoder.decode(chunk) if isinstance(chunk, bytes) else str(chunk)
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            line = line.rstrip("\r")
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            if line or not data_lines:
                continue
            data = "\n".join(data_lines)
            data_lines.clear()
            if data == "[DONE]":
                return
            if data:
                event = json.loads(data)
                if "error" in event:
                    error = event["error"]
                    raise RelayError(error.get("message", "provider stream failed"),
                                     error.get("type", "provider_http_error"))
                yield event
    raise RelayError("Provider stream ended without [DONE]", "provider_connectivity")


def coalesce_sse(chunks):
    """Turn a streamed chat completion into the legacy complete JSON shape."""
    content = []
    reasoning = []
    role = "assistant"
    finish_reason = None
    first = {}
    for event in iter_sse_events(chunks):
        if not first:
            first = dict(event)
        choice = (event.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        role = delta.get("role", role)
        if delta.get("content"):
            content.append(delta["content"])
        # Some Tencent-compatible endpoints emit the answer in
        # reasoning_content even for short prompts. Preserve visible content
        # when present, and use reasoning as a compatibility fallback only
        # when the provider emitted no content channel at all.
        if delta.get("reasoning_content"):
            reasoning.append(delta["reasoning_content"])
        if choice.get("finish_reason") is not None:
            finish_reason = choice["finish_reason"]
    if not content and reasoning:
        content = reasoning
    if not content:
        raise RelayError("Provider stream has no complete content", "provider_connectivity")
    first["choices"] = [{"index": 0, "message": {"role": role, "content": "".join(content)},
                         "finish_reason": finish_reason}]
    return first or {"choices": [{"message": {"role": role, "content": ""}}]}


class TencentSandboxTransport:
    """Synchronous transport facade over one persistent AsyncSandbox."""

    def __init__(self, *, template: str, api_key: str, api_base: str,
                 metadata: dict[str, Any] | None = None, timeout: int = 86_400,
                 request_timeout: int = 180):
        if not template:
            raise ValueError("TBENCH_E2B_RELAY_TEMPLATE is required")
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required for the E2B relay")
        self.template, self.api_key, self.api_base = template, api_key, api_base.rstrip("/")
        self.metadata = metadata or {}
        self.timeout, self.request_timeout = timeout, request_timeout
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="tencent-relay-loop")
        self._ready = threading.Event()
        self._sandbox = None
        # Protects sandbox replacement only. Provider commands must be able to
        # overlap; the experiment's worker cap is enforced by this semaphore.
        self._sandbox_lock = None
        # Keep the historical cap by default, while allowing an explicitly
        # authorized run to raise the local request concurrency.
        self.max_concurrency = max(1, int(os.environ.get("TBENCH_RELAY_MAX_CONCURRENCY", "100")))
        self._request_slots = threading.BoundedSemaphore(self.max_concurrency)
        self._closing = threading.Event()
        self._active_condition = threading.Condition()
        self._active_requests = set()
        self.sandbox_id = None
        self._thread.start()
        self._ready.wait(timeout=30)
        if self._sandbox is None:
            raise RelayError("E2B relay sandbox failed to start", "provider_connectivity")

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._sandbox_lock = asyncio.Lock()
        try:
            self._sandbox = self._loop.run_until_complete(self._create())
            self.sandbox_id = self._sandbox.sandbox_id
        except Exception:
            LOG.exception("failed to create Tencent relay sandbox")
        finally:
            self._ready.set()
        self._loop.run_forever()

    async def _create(self):
        from e2b import AsyncSandbox
        metadata = dict(self.metadata)
        metadata.setdefault("environment_name", "skillexpand-llm-relay")
        metadata.setdefault("session_id", "skillexpand-relay-" + uuid.uuid4().hex[:12])
        # Older relay templates need an explicit envd command, while newer
        # healthy templates already carry a valid startup configuration.  Do
        # not overwrite the latter: the forced image/command can make a valid
        # template fail with ContainerStart before any provider request runs.
        if os.environ.get("TBENCH_E2B_RELAY_USE_TEMPLATE_DEFAULT", "0").lower() not in {
            "1", "true", "yes", "on"
        }:
            metadata.setdefault("x-custom-config", json.dumps({
                "image": os.environ.get(
                    "TBENCH_E2B_RELAY_IMAGE",
                    "modelbest.tencentcloudcr.com/terminalbench/runtime:fix-git_20260403",
                ),
                "imageRegistryType": "enterprise",
                "command": ["/bin/sh", "-c"],
                "args": ["/mnt/envd -port 49983"],
                "resources": {
                    "cpu": os.environ.get("TBENCH_E2B_RELAY_CPU", "2"),
                    "memory": os.environ.get("TBENCH_E2B_RELAY_MEMORY", "4Gi"),
                },
            }))
        metadata.setdefault("x-mounts", json.dumps([{
            "name": "cfs", "mountPath": "/mnt/cfs/agent-tools",
            "subPath": "agent-tools", "readOnly": True,
        }]))
        return await AsyncSandbox.create(
            template=self.template, metadata=metadata, timeout=self.timeout,
            allow_internet_access=True,
            envs={"OPENAI_API_KEY": self.api_key, "MODEL_API_BASE": self.api_base,
                  "RELAY_PROVIDER_TIMEOUT": str(self.request_timeout)},
            api_key=os.environ.get("E2B_API_KEY"),
            validate_api_key=False,
            domain=os.environ.get("E2B_DOMAIN", "ap-beijing.tencentags.com"),
        )

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Compatibility API that consumes the strict stream to completion."""
        payload = dict(payload)
        payload["stream"] = True
        return coalesce_sse(self.stream(payload))

    def stream(self, payload: dict[str, Any]):
        """Yield raw provider SSE bytes through the E2B command stdout stream."""
        payload = dict(payload)
        default_limit = default_output_token_limit(payload.get('model', ''))
        if default_limit is not None and payload.get('max_tokens') is None:
            payload['max_tokens'] = default_limit
        request_id = uuid.uuid4().hex
        started = time.monotonic()
        path = PurePosixPath("/tmp") / f"skillexpand-relay-{request_id}.json"
        queue = Queue()
        sentinel = object()
        admitted_at = time.monotonic()
        if self._closing.is_set() or not self._request_slots.acquire(timeout=self.request_timeout):
            raise RelayError("relay request admission timed out", "relay_queue_timeout")
        with self._active_condition:
            self._active_requests.add(request_id)
        admitted_at = time.monotonic() - admitted_at
        metrics = {"queue_wait_ms": int(admitted_at * 1000), "chunks": 0,
                   "first_chunk_ms": None, "first_token_ms": None}

        def emit(chunk):
            now = time.monotonic()
            metrics["chunks"] += 1
            metrics["first_chunk_ms"] = metrics["first_chunk_ms"] or int((now - started) * 1000)
            if chunk and metrics["first_token_ms"] is None:
                metrics["first_token_ms"] = int((now - started) * 1000)
            queue.put(chunk)

        async def run():
            try:
                await self._request_stream(path, json.dumps(payload, ensure_ascii=False).encode(), emit)
                queue.put(sentinel)
            except Exception as exc:
                queue.put(exc)
                queue.put(sentinel)

        future = None
        try:
            future = asyncio.run_coroutine_threadsafe(run(), self._loop)
            while True:
                try:
                    item = queue.get(timeout=self.request_timeout + 30)
                except Empty as exc:
                    raise RelayError("Tencent relay request timed out", "provider_timeout") from exc
                if item is sentinel:
                    LOG.info("relay request_id=%s model=%s sandbox=%s status=200 latency_ms=%d metrics=%s",
                             request_id, payload.get("model"), self.sandbox_id,
                             int((time.monotonic() - started) * 1000), metrics)
                    return
                if isinstance(item, Exception):
                    error_class = getattr(item, "error_class", classify_provider_error(item))
                    LOG.warning("relay request_id=%s model=%s sandbox=%s error_class=%s latency_ms=%d metrics=%s",
                                request_id, payload.get("model"), self.sandbox_id, error_class,
                                int((time.monotonic() - started) * 1000), metrics)
                    if isinstance(item, RelayError):
                        raise item
                    raise RelayError(f"Tencent relay request failed: {error_class}", error_class) from item
                yield item
        finally:
            if future is not None and not future.done():
                future.cancel()
            self._request_slots.release()
            with self._active_condition:
                self._active_requests.discard(request_id)
                self._active_condition.notify_all()

    async def _request_stream(self, path, data: bytes, emit) -> None:
        # Renew the sandbox lease before a host-role request. Harbor tasks can
        # take longer than the relay's idle lifetime, so recreate only when the
        # control plane confirms that the previous sandbox no longer exists.
        async with self._sandbox_lock:
            sandbox = self._sandbox
            try:
                awaitable = sandbox.set_timeout(self.timeout)
                # The event-loop thread owns the sandbox, but lease renewal is
                # serialized so concurrent requests never replace a healthy
                # instance underneath one another.
                await awaitable
            except Exception as exc:
                LOG.warning("relay sandbox lease renewal failed: %s", type(exc).__name__)
                message = str(exc).lower()
                sandbox_missing = any(token in message for token in
                                      ("not found", "does not exist", "no such sandbox", "404"))
                if not sandbox_missing:
                    raise RelayError("relay sandbox lease renewal failed", "provider_connectivity") from exc
                sandbox = await self._create()
                self._sandbox = sandbox
                self.sandbox_id = sandbox.sandbox_id
        await sandbox.files.write(str(path), data, user="root")
        handle = None
        try:
            handle = await sandbox.commands.run(
                cmd="python3 -u -c " + shlex.quote(_RELAY_SCRIPT) + " " + shlex.quote(str(path)),
                user="root", background=True, timeout=0, on_stdout=emit)
            await handle.wait()
        except Exception as exc:
            stderr = getattr(exc, "stderr", "") or ""
            detail = str(exc).replace(self.api_key, '[REDACTED]') if self.api_key else str(exc)
            safe_stderr = stderr.replace(self.api_key, '[REDACTED]') if self.api_key else stderr
            LOG.warning("relay command failure exception_type=%s exit_code=%s detail=%r stderr=%r",
                        type(exc).__name__, getattr(exc, 'exit_code', None),
                        detail[:1500], safe_stderr[-2000:])
            provider_error = command_provider_error(safe_stderr)
            if provider_error is not None:
                raise provider_error from exc
            if stderr:
                raise RelayError("Tencent provider request failed", "provider_http_error") from exc
            raise
        finally:
            if handle is not None and handle.exit_code is None:
                try:
                    await handle.kill()
                    await handle.disconnect()
                except Exception:
                    LOG.warning("relay command cleanup failed")
            try:
                await sandbox.files.remove(str(path), user="root")
            except Exception:
                pass

    def close(self):
        self._closing.set()
        deadline = time.monotonic() + 30
        with self._active_condition:
            while self._active_requests and time.monotonic() < deadline:
                self._active_condition.wait(timeout=max(0, deadline - time.monotonic()))
        if self._sandbox is not None:
            future = asyncio.run_coroutine_threadsafe(self._sandbox.kill(), self._loop)
            try:
                future.result(timeout=30)
            finally:
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join(timeout=30)
                self._sandbox = None


class _Handler(BaseHTTPRequestHandler):
    relay: "TencentSandboxLLMRelay"
    def do_GET(self):
        if self.path == "/v1/models":
            self._json({"object": "list", "data": []})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404); return
        length = int(self.headers.get("Content-Length", "0"))
        headers_sent = False
        stream = None
        try:
            payload = json.loads(self.rfile.read(length))
            if not payload.get("stream"):
                self._json(self.relay.transport.request(payload))
                return
            stream = iter(self.relay.transport.stream(payload))
            # Obtain the first event before committing the HTTP status.
            first = next(stream)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            headers_sent = True
            def chunks():
                yield first
                yield from stream
            content_chars = reasoning_chars = 0
            finish_reasons = []
            for event in iter_sse_events(chunks()):
                # Old LangChain requires choices[0] even for usage-only chunks.
                if not event.get("choices"):
                    continue
                for choice in event["choices"]:
                    delta = choice.get("delta")
                    if choice.get("finish_reason") is not None:
                        finish_reasons.append(choice["finish_reason"])
                    if isinstance(delta, dict):
                        content_chars += len(delta.get("content") or "")
                        reasoning_chars += len(delta.get("reasoning_content") or "")
                    if isinstance(delta, dict) and delta.get("content") is None:
                        delta["content"] = ""
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode())
                self.wfile.flush()
            if not content_chars:
                kind = ('provider_output_truncated' if 'length' in finish_reasons
                        else 'provider_empty_content')
                raise RelayError(
                    f"Provider returned no visible answer: content_chars={content_chars}, "
                    f"reasoning_chars={reasoning_chars}, finish_reasons={finish_reasons}", kind, 502)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except RelayError as exc:
            error = {"error": {"message": str(exc), "type": exc.error_class}}
            if headers_sent:
                self.wfile.write(("data: " + json.dumps(error) + "\n\n").encode())
                self.wfile.flush()
            else:
                self._json(error, exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            if not headers_sent:
                self._json({"error": {"message": "relay request failed", "type": "provider_error"}}, 500)
            LOG.exception("unclassified relay error: %s", type(exc).__name__)
        finally:
            if stream is not None:
                stream.close()

    def _json(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def log_message(self, *_):
        return


class TencentSandboxLLMRelay:
    def __init__(self, transport: TencentSandboxTransport, host="127.0.0.1", port=0):
        self.transport = transport
        self.server = ThreadingHTTPServer((host, port), _Handler)
        _Handler.relay = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="tencent-relay-http")

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def start(self): self.thread.start(); return self.base_url
    def close(self):
        self.server.shutdown(); self.server.server_close(); self.transport.close()


def relay_from_env() -> TencentSandboxLLMRelay:
    raw_metadata = os.environ.get("TBENCH_E2B_RELAY_METADATA_JSON", "{}")
    try:
        metadata = json.loads(raw_metadata)
    except json.JSONDecodeError as exc:
        raise ValueError("TBENCH_E2B_RELAY_METADATA_JSON is not valid JSON") from exc
    if not isinstance(metadata, dict):
        raise ValueError("TBENCH_E2B_RELAY_METADATA_JSON must be an object")
    transport = TencentSandboxTransport(
        template=os.environ.get("TBENCH_E2B_RELAY_TEMPLATE") or
                 os.environ.get("TB2_E2B_TEMPLATE", ""),
        api_key=os.environ.get("OPENAI_API_KEY", ""),
        api_base=os.environ.get("TBENCH_RELAY_PROVIDER_BASE", "https://llm-center.modelbest.co/v1"),
        metadata=metadata,
        timeout=int(os.environ.get("TBENCH_E2B_RELAY_TIMEOUT_SECONDS", "86400")),
        request_timeout=int(os.environ.get("EXPE_LLM_TIMEOUT_SECONDS", "180")),
    )
    return TencentSandboxLLMRelay(transport)
