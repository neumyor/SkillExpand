"""Unit-boundary failure records and their stage-level collection.

A *unit* is one task/request a stage schedules independently (an L1 task, a
route, a fixed-Skill execution, a predicted judgment).  Workers never raise:
:func:`guard` turns an exception into a ``unit-error-v1`` record, which crosses
process boundaries as plain data.  The stage feeds records to a
:class:`FailureCollector`, which persists them and applies the category's
disposition: retryable failures are gathered and raised together as
:class:`StageIncomplete`; anything else halts the stage immediately.
"""
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from skillexpand.persistence.io import save
from skillexpand.reliability.errors import (
    DISPOSITIONS, Category, Halt, StageIncomplete, UnitFailed, classify, translate,
)

SCHEMA = 'unit-error-v1'
MESSAGE_LIMIT = 1000


def exit_now(code: int) -> None:
    """Terminate without joining worker threads still finishing in-flight requests.

    Used after a halting failure has been recorded: waiting for a congested
    endpoint would delay the halt (and the supervisor's sibling shutdown).
    """
    import os
    import sys
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def failure_record(exc: BaseException, *, unit_id: Any, stage: str) -> Dict[str, Any]:
    category = classify(exc)
    shown = translate(exc) or exc
    message = ' '.join(str(shown).split())
    if len(message) > MESSAGE_LIMIT:
        message = message[:MESSAGE_LIMIT - 3] + '...'
    causes, current = [], exc
    while current is not None:
        causes.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    return {
        'schema': SCHEMA, 'unit_id': unit_id, 'stage': stage,
        'category': category.value, 'type': type(shown).__name__,
        'retryable': DISPOSITIONS[category].retryable, 'message': message,
        'cause_chain': causes, 'time': time.time(),
    }


def guard(call: Callable[[], Any], *, unit_id: Any, stage: str):
    """Return ``(value, None)`` or ``(None, failure_record)``; never raises ``Exception``."""
    try:
        return call(), None
    except Exception as exc:  # noqa: BLE001 - the record carries the category
        return None, failure_record(exc, unit_id=unit_id, stage=stage)


class FailureCollector:
    """Persist unit failures for one stage and apply their dispositions."""

    def __init__(self, stage: str, directory: Optional[Path] = None):
        self.stage = stage
        self.directory = Path(directory) if directory is not None else None
        self.failures: List[Dict[str, Any]] = []

    def record(self, failure: Dict[str, Any], name: Optional[str] = None,
               evidence: Optional[Dict[str, Any]] = None) -> None:
        """Persist ``failure`` (with optional partial ``evidence``) and apply its disposition."""
        if failure.get('schema') != SCHEMA:
            # A worker that bypassed guard() is itself a defect.
            failure = dict(failure, schema=SCHEMA, category=Category.BUG.value, retryable=False)
        failure = dict(failure, stage=self.stage)
        if self.directory is not None:
            stored = dict(failure, evidence=evidence) if evidence is not None else failure
            save(self.directory / f"{name if name is not None else failure['unit_id']}.json",
                 stored)
        if DISPOSITIONS[Category(failure['category'])].halt is not Halt.AFTER_STAGE:
            raise UnitFailed(failure)
        self.failures.append(failure)

    def record_exception(self, exc: BaseException, *, unit_id: Any,
                         name: Optional[str] = None) -> None:
        self.record(failure_record(exc, unit_id=unit_id, stage=self.stage), name)

    def raise_if_incomplete(self, message: str, error=StageIncomplete) -> None:
        if self.failures:
            raise error(f'{message} ({len(self.failures)} unit(s) failed; resume retries them)',
                        self.failures)


def map_units(units, call: Callable[[Any], Any], *, workers: int, collector: FailureCollector,
              on_success: Callable[[Any, Any], None], name: Callable[[Any], str] = str,
              thread_name_prefix: str = 'unit') -> None:
    """Run in-process units on a bounded thread pool under ``collector``.

    Results are consumed in submission order so persisted state is
    reproducible.  A failure that halts the stage cancels queued units.
    """
    from concurrent.futures import ThreadPoolExecutor

    units = list(units)
    if not units:
        return
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(units))),
                            thread_name_prefix=thread_name_prefix) as pool:
        futures = [(unit, pool.submit(call, unit)) for unit in units]
        try:
            for unit, future in futures:
                try:
                    value = future.result()
                except Exception as exc:  # noqa: BLE001 - disposition decides
                    collector.record_exception(exc, unit_id=unit, name=name(unit))
                else:
                    on_success(unit, value)
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
