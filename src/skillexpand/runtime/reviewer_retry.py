"""Shared recovery for Reviewer responses without changing request prompts."""

import logging
import os
import time


DEFAULT_ATTEMPTS = 32
LOGGER = logging.getLogger(__name__)


class ReviewerOutputError(ValueError):
    """A generated response failed its output contract."""


class ReviewerUpdateError(RuntimeError):
    """A resumable Reviewer request exhausted its local recovery budget."""


def retry_reviewer(call, parse, *, attempts=None):
    budget = int(os.environ.get("EXPE_REVIEWER_ATTEMPTS", DEFAULT_ATTEMPTS)
                 if attempts is None else attempts)
    if budget < 1:
        raise ValueError("Reviewer attempt budget must be positive")
    for attempt in range(1, budget + 1):
        # Request/authentication errors and programming errors propagate. The
        # provider wrapper already owns recovery for network failures.
        raw = call()
        try:
            return parse(raw), attempt
        except ReviewerOutputError as exc:
            if attempt == budget:
                raise ReviewerUpdateError(
                    f"Reviewer output invalid after {budget} attempts: {exc}"
                ) from exc
            delay = min(30, 2 ** min(attempt - 1, 5))
            LOGGER.warning("Reviewer output retry %s/%s in %ss: %s",
                           attempt, budget, delay, exc)
            time.sleep(delay)
