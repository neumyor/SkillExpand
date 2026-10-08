"""The two retry loops: transient infrastructure and model-output repair."""
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple

from skillexpand.reliability.errors import (
    InfrastructureError, RepairExhausted, ResponseFormatError, SchemaViolation, translate,
)
from skillexpand.reliability.policies import EXHAUSTED_DEGRADE, RepairPolicy, RetryPolicy

LOGGER = logging.getLogger(__name__)

#: Exceptions a response parser raises for malformed model output.  A parser is
#: pure validation of untrusted, arbitrarily shaped JSON, so these describe the
#: response.  Exceptions from the *request* are never treated this way.
PARSE_ERRORS = (ValueError, KeyError, TypeError, AttributeError)


def retry_transient(call: Callable[[], Any], policy: RetryPolicy, *,
                    sleep: Optional[Callable[[float], None]] = None) -> Any:
    """Repeat ``call`` while it fails with a (translated) ``InfrastructureError``.

    Other failures propagate, translated into the taxonomy when a translation
    is registered and unchanged otherwise.
    """
    retry = 0
    while True:
        try:
            return call()
        except Exception as exc:
            error = translate(exc)
            exhausted = policy.attempts is not None and retry + 1 >= policy.attempts
            if not isinstance(error, InfrastructureError) or exhausted:
                if error is None or error is exc:
                    raise
                raise error from exc
            LOGGER.warning('%s retry %s after %s', policy.name, retry + 1, error)
            (sleep or time.sleep)(policy.delay(retry))
            retry += 1


@dataclass(frozen=True)
class FailedAttempt:
    attempt: int
    raw: Any
    error: ResponseFormatError


@dataclass
class RepairResult:
    value: Any
    #: Total attempts made, including replayed cached responses.
    attempts: int
    failures: List[FailedAttempt] = field(default_factory=list)
    degraded: bool = False

    @property
    def last_failure(self) -> Optional[FailedAttempt]:
        return self.failures[-1] if self.failures else None


def call_with_repair(policy: RepairPolicy,
                     request: Callable[[int, Optional[FailedAttempt]], Tuple[Any, bool]],
                     parse: Callable[[Any], Any], *,
                     sleep: Optional[Callable[[float], None]] = None) -> RepairResult:
    """Request and validate model output under ``policy``.

    ``request(attempt, previous_failure)`` returns ``(raw, fresh)``; ``fresh`` is
    False when the response was replayed from a durable cache.  Replayed
    responses are validated again but only fresh calls consume the budget, so a
    resumed unit resamples instead of replaying the same invalid output.

    Exceptions raised by ``request`` (provider, environment, programming
    errors) propagate unchanged: only the parser's output errors are repaired.
    """
    failures: List[FailedAttempt] = []
    fresh_calls = 0
    attempt = 0
    while True:
        raw, fresh = request(attempt, failures[-1] if failures else None)
        fresh_calls += bool(fresh)
        try:
            return RepairResult(parse(raw), attempt + 1, failures)
        except ResponseFormatError as exc:
            error = exc
        except PARSE_ERRORS as exc:
            error = SchemaViolation(str(exc))
            error.__cause__ = exc
        failures.append(FailedAttempt(attempt, raw, error))
        if fresh_calls >= policy.attempts:
            if policy.exhausted == EXHAUSTED_DEGRADE:
                return RepairResult(None, attempt + 1, failures, degraded=True)
            raise RepairExhausted(
                f'{policy.name}: output invalid after {fresh_calls} attempts: {error}',
                policy=policy.name, failures=failures) from error
        if fresh:
            LOGGER.warning('%s output retry %s/%s: %s', policy.name, fresh_calls,
                           policy.attempts, error)
            (sleep or time.sleep)(policy.delay(fresh_calls - 1))
        attempt += 1


def fresh(call: Callable[[], Any]) -> Callable[[int, Optional[FailedAttempt]], Tuple[Any, bool]]:
    """Adapt an uncached request that ignores the previous failure."""
    return lambda attempt, previous: (call(), True)
