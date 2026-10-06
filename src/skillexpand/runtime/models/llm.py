from typing import Callable, List
import fcntl
import json
import os
import time
import math
import threading

from langchain.chat_models import ChatOpenAI
from langchain.schema import ChatMessage
import openai


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
        if not 0 <= value <= 10:
            raise ValueError()
        return value
    except ValueError as exc:
        raise ValueError(f'{name} must be an integer from 0 to 10') from exc


def request_policy():
    timeout = float(os.environ.get('EXPE_LLM_TIMEOUT_SECONDS', '300'))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('EXPE_LLM_TIMEOUT_SECONDS must be finite and positive')
    # ``EXPE_LLM_RETRIES`` remains in the manifest for backwards-compatible
    # environment validation, but transient provider failures are now retried
    # until success by GPTWrapper.  The field is retained as metadata so old
    # manifests can still be audited without silently changing their identity.
    configured_retries = _positive_int_env('EXPE_LLM_RETRIES', 2)
    return {
        'timeout': timeout,
        'retries': configured_retries,
        'retry_forever': True,
        'retry_backoff_max': 60,
    }


def retry_delay(attempt: int) -> int:
    """Seconds before retry ``attempt`` (zero-based), capped at one minute."""
    if attempt < 0:
        raise ValueError('attempt must be non-negative')
    return 60 if attempt >= 6 else 2 ** attempt


def wait_for_request_slot():
    path = os.environ.get('EXPE_LLM_GATE_FILE')
    if not path:
        return
    interval = float(os.environ.get('EXPE_LLM_REQUEST_INTERVAL_SECONDS', '0'))
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError('EXPE_LLM_REQUEST_INTERVAL_SECONDS must be finite and positive')
    with open(path, 'a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        previous = stream.read().strip()
        deadline = float(previous) if previous else 0.0
        time.sleep(max(0, deadline - time.time()))
        stream.seek(0)
        stream.truncate()
        stream.write(str(time.time() + interval))
        stream.flush()


#: Environment variable pointing at an OpenAI-compatible endpoint (e.g. a local
#: vLLM server or a hosted gateway). When unset, ExpeL behaves exactly as
#: upstream: OpenAI only.
BASE_URL_ENV_VAR = 'EXPE_LLM_BASE_URL'

#: Set truthy to send ``enable_thinking: false``.  Required for reasoning models.
THINKING_ENV_VAR = 'EXPE_LLM_DISABLE_THINKING'

#: Arbitrary extra request fields, as a JSON object.  Escape hatch for other
#: provider-specific switches.
EXTRA_ENV_VAR = 'EXPE_LLM_EXTRA_JSON'

_WARNED = set()


def get_llm_base_url() -> str:
    """OpenAI-compatible base URL, or None for upstream (real OpenAI) behaviour."""
    return os.environ.get(BASE_URL_ENV_VAR) or os.environ.get('OPENAI_API_BASE') or None


def _truthy(name: str) -> bool:
    return os.environ.get(name, '').strip().lower() in ('1', 'true', 'yes', 'on')


def _model_list_env(name: str) -> set[str]:
    return {item.strip() for item in os.environ.get(name, '').split(',') if item.strip()}


def thinking_request_kwargs(model_name: str = None) -> dict:
    """Return the provider thinking switch for one requested model.

    The campaign endpoint exposes both a Qwen model that must receive
    ``enable_thinking=false`` and a GLM model that rejects that value and
    requires thinking to remain enabled.  The explicit allow-list keeps this
    choice tied to the frozen model map instead of silently applying one
    provider's setting to every role.
    """
    enabled = _model_list_env('EXPE_LLM_ENABLE_THINKING_MODELS')
    if model_name and model_name in enabled:
        return {'enable_thinking': True}
    if _truthy(THINKING_ENV_VAR):
        return {'enable_thinking': False}
    return {}


def accepted_reported_model_names(requested: str) -> set[str]:
    """Return request/canonical names accepted by hosted OpenAI gateways."""
    aliases = {requested}
    for suffix in ('-distill', '-ali', '-tianyi', '-xunya', '-tencent'):
        if requested.endswith(suffix):
            aliases.add(requested[:-len(suffix)])
    return aliases


def get_extra_model_kwargs(model_name: str = None) -> dict:
    """Extra fields merged into every chat request.

    ``enable_thinking: false`` is not cosmetic for a reasoning model.  Measured on
    qwen3.6-flash-distill with this prompt shape:

    ==========================================  =========  ===============
    configuration                               latency    content
    ==========================================  =========  ===============
    thinking on                                 ~14.5 s    coherent
    thinking on + ``stop=['\\n','\\n\\n']``        ~7.8 s    **empty string**
    thinking off                                ~1.3 s     coherent
    ==========================================  =========  ===============

    ExpeL calls the model with ``stop=['\\n', '\\n\\n']`` for thoughts and
    reflections, so with thinking left on the stop sequence fires inside the
    reasoning channel and the visible ``content`` comes back empty.  That does not
    raise -- it silently turns every thought and every reflection into '', so the
    agent's reasoning disappears and the reflective loop becomes a no-op.  It also
    costs roughly 11x the latency and tokens.

    Returned fields are additionally useful because langchain's ``ChatOpenAI``
    forwards ``model_kwargs`` verbatim into the request body, which is the only
    supported way to reach provider-specific switches here.
    """
    kwargs = thinking_request_kwargs(model_name)
    raw = os.environ.get(EXTRA_ENV_VAR)
    if raw:
        try:
            kwargs.update(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise ValueError(f'{EXTRA_ENV_VAR} is not valid JSON: {exc}') from exc
    return kwargs


def warn_if_reasoning_trap() -> None:
    """Loudly flag the configuration that silently empties every response."""
    base_url = get_llm_base_url()
    if base_url is None or _truthy(THINKING_ENV_VAR) or EXTRA_ENV_VAR in _WARNED:
        return
    _WARNED.add(EXTRA_ENV_VAR)
    print(
        'WARNING: EXPE_LLM_BASE_URL is set but EXPE_LLM_DISABLE_THINKING is not.\n'
        '         If the served model is a reasoning model, ExpeL\'s\n'
        "         stop=['\\n','\\n\\n'] calls will return an EMPTY content string and\n"
        '         every thought/reflection will silently become empty. Set\n'
        '         EXPE_LLM_DISABLE_THINKING=1 to send enable_thinking=false.',
        flush=True)


class GPTWrapper:
    """ChatOpenAI wrapper.

    With no base URL configured this is upstream ExpeL unchanged. With one
    configured it talks to any OpenAI-compatible server (vLLM, a hosted gateway).
    """

    def __init__(self, llm_name: str, openai_api_key: str, long_ver: bool,
                 base_url: str = None):
        self.model_name = llm_name
        self.base_url = base_url
        self.request_policy = request_policy()
        if long_ver and base_url is None:
            # Upstream's long-context fallback only exists for legacy OpenAI
            # models. A self-hosted or proxied deployment serves a single model
            # with its own context window, so rewriting the model name would 404.
            llm_name = 'gpt-3.5-turbo-16k'
        kwargs = dict(
            model=llm_name,
            temperature=0.0,
            openai_api_key=openai_api_key,
            max_retries=0,
            request_timeout=self.request_policy['timeout'],
        )
        if base_url is not None:
            kwargs['openai_api_base'] = base_url
            # A remote server needs no client-side retry storm; keep it quick so a
            # dead endpoint fails loudly instead of hanging for minutes.
            # The wrapper owns the retry budget so a provider timeout cannot be
            # multiplied by both the client and wrapper retry loops.
            extra = get_extra_model_kwargs(llm_name)
            if extra:
                kwargs['model_kwargs'] = extra
        self.llm = ChatOpenAI(**kwargs)
        # LangChain 0.0.x exposes request fields through ``model_kwargs`` rather
        # than per-call keyword arguments.  Keep temporary reviewer-specific
        # fields isolated when a host is ever shared by worker threads.
        self._request_kwargs_lock = threading.RLock()

    def __call__(self, messages: List[ChatMessage], stop: List[str] = [],
                 replace_newline: bool = True, request_kwargs: dict = None) -> str:
        kwargs = {}
        if stop != []:
            kwargs['stop'] = stop
        # Upstream retried only on RateLimitError. A locally-served model behind
        # a tunnel can also raise connection/timeout errors, which used to abort
        # a whole run on a single transient blip.
        retryable = (openai.error.RateLimitError, openai.error.APIError,
                     openai.error.Timeout, openai.error.APIConnectionError)
        attempt = 0
        while True:
            try:
                wait_for_request_slot()
                with self._request_kwargs_lock:
                    existing_model_kwargs = getattr(self.llm, 'model_kwargs', {})
                    # Test doubles and a few older LangChain clients do not
                    # expose a real dict here.
                    if not isinstance(existing_model_kwargs, dict):
                        existing_model_kwargs = {}
                    previous_model_kwargs = dict(existing_model_kwargs)
                    if request_kwargs:
                        merged = dict(previous_model_kwargs)
                        merged.update(request_kwargs)
                        self.llm.model_kwargs = merged
                    try:
                        message = self.llm(messages, **kwargs)
                    finally:
                        if request_kwargs:
                            self.llm.model_kwargs = previous_model_kwargs
                output = str(message.content or '').strip('\n').strip()
                break
            except retryable:
                # Network/provider outages are transient at this layer.  Keep
                # retrying forever so a scheduler restart is not required just
                # because a tunnel or endpoint was unavailable for a few minutes.
                time.sleep(retry_delay(attempt))
                attempt += 1

        if replace_newline:
            output = output.replace('\n', '')
        return output


def LLM_CLS(llm_name: str, openai_api_key: str, long_ver: bool) -> Callable:
    base_url = get_llm_base_url()
    if base_url is not None:
        warn_if_reasoning_trap()
        # Any model name is accepted: it is whatever the endpoint advertises. An
        # API key is still required by the OpenAI client library.
        return GPTWrapper(llm_name, openai_api_key or 'EMPTY', False, base_url=base_url)
    if 'gpt' in llm_name:
        return GPTWrapper(llm_name, openai_api_key, long_ver)
    else:
        raise ValueError(f"Unknown LLM model name: {llm_name}")
