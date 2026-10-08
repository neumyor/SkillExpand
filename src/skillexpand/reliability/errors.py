"""Failure categories, their dispositions, and translation of library exceptions.

Every exception that can stop a unit belongs to one :class:`Category`.  What a
category *means* -- whether the unit is retried and what stops -- is decided
once in :data:`DISPOSITIONS`, never at the raise site.

Extending the taxonomy when a new failure is observed:

* A library exception with a known meaning: call
  ``register_translation(LibraryError, ProviderUnavailable)`` next to the code
  that calls the library.  Retry loops and unit records pick it up.  When the
  meaning depends on the instance (an HTTP status), pass a function returning
  the target class and declare whether it can be ``transient``.
* A new kind of failure: subclass the closest class below.  Introduce a new
  :class:`Category` only if it needs a different disposition, and add that
  disposition to :data:`DISPOSITIONS`.

Anything that is not a :class:`SkillExpandError` and has no registered
translation is a programming error (:attr:`Category.BUG`): it is never turned
into data, and it halts the whole run.
"""
import enum
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple, Type, Union


class Category(str, enum.Enum):
    INFRASTRUCTURE = 'infrastructure'
    RESPONSE = 'response'
    PROVIDER_REJECTED = 'provider_rejected'
    INTEGRITY = 'integrity'
    CONFIGURATION = 'configuration'
    BUG = 'bug'


class Halt(str, enum.Enum):
    #: Record the unit; other units continue; the stage then raises a retryable
    #: :class:`StageIncomplete`, and a resumed stage redoes only failed units.
    AFTER_STAGE = 'after_stage'
    #: Stop the current stage now; resuming without intervention cannot help.
    STAGE = 'stage'
    #: Stop everything, including sibling campaign jobs.
    ALL = 'all'


@dataclass(frozen=True)
class Disposition:
    retryable: bool
    halt: Halt


DISPOSITIONS: Dict[Category, Disposition] = {
    Category.INFRASTRUCTURE: Disposition(True, Halt.AFTER_STAGE),
    Category.RESPONSE: Disposition(True, Halt.AFTER_STAGE),
    Category.PROVIDER_REJECTED: Disposition(False, Halt.STAGE),
    Category.INTEGRITY: Disposition(False, Halt.STAGE),
    Category.CONFIGURATION: Disposition(False, Halt.STAGE),
    Category.BUG: Disposition(False, Halt.ALL),
}


class SkillExpandError(Exception):
    """Base class; ``category`` decides the disposition."""

    category = Category.BUG

    @property
    def disposition(self) -> Disposition:
        return DISPOSITIONS[self.category]

    @property
    def retryable(self) -> bool:
        return self.disposition.retryable


# -- infrastructure: the work is valid, the service or machine was not -------

class InfrastructureError(SkillExpandError):
    category = Category.INFRASTRUCTURE


class ProviderUnavailable(InfrastructureError):
    """Network, timeout, rate limit, overload or 5xx from a model/service endpoint."""


class EnvironmentFailure(InfrastructureError):
    """A benchmark environment or its backend failed, independent of the agent."""


class EnvironmentTimeout(EnvironmentFailure, TimeoutError):
    """A native environment call exceeded its deadline."""


class WorkerLost(InfrastructureError, TimeoutError):
    """A process pool made no progress or lost a worker."""


class StageIncomplete(SkillExpandError, RuntimeError):
    """A stage finished its batch with retryable unit failures.

    Only retryable failures reach this point: non-retryable ones halt the stage
    as soon as they are recorded.  ``failures`` holds the unit records.
    """

    def __init__(self, message: str, failures=()):
        super().__init__(message)
        self.failures = list(failures)
        categories = {f.get('category') for f in self.failures}
        self.category = (Category.RESPONSE if categories == {Category.RESPONSE.value}
                         else Category.INFRASTRUCTURE)


# -- provider refused the request: retrying the same request cannot help ----

class ProviderRejected(SkillExpandError):
    """Authentication, permission, unknown model, invalid or oversized request."""

    category = Category.PROVIDER_REJECTED


# -- model output did not satisfy its contract --------------------------------

class ResponseFormatError(SkillExpandError, ValueError):
    category = Category.RESPONSE


class JsonExtractionError(ResponseFormatError):
    """No complete JSON object could be extracted."""


class SchemaViolation(ResponseFormatError):
    """Missing/extra fields, wrong types or out-of-range values."""


class ReferenceViolation(SchemaViolation):
    """References to card, evidence, rule or feedback IDs that were not supplied."""


class RepairExhausted(ResponseFormatError):
    """The repair policy's budget ended without a valid response."""

    def __init__(self, message: str, policy: str = '', failures=()):
        super().__init__(message)
        self.policy = policy
        self.failures = list(failures)


# -- stored results disagree with their frozen definition --------------------

class IntegrityError(SkillExpandError, ValueError):
    category = Category.INTEGRITY


class FrozenProtocolChanged(IntegrityError):
    """A frozen identity's protocol fields differ on resume."""


class FrozenCodeChanged(IntegrityError):
    """Only the source fingerprint of a frozen identity differs."""


class AuditFailure(IntegrityError):
    """An offline audit found results inconsistent with their evidence."""


class JournalConflict(IntegrityError):
    """A journal, frozen plan, cache or unit result conflicts with another."""


class StoreError(IntegrityError):
    """Persisted state is internally inconsistent."""


class LedgerCorrupt(StoreError):
    """A JSONL ledger is corrupt before its final record."""


class IsolationViolation(IntegrityError):
    """Train/val/test isolation or an experience-withholding contract was broken."""


# -- the run cannot start as configured ---------------------------------------

class ConfigurationError(SkillExpandError, ValueError):
    category = Category.CONFIGURATION


class InvalidInput(ConfigurationError):
    """Arguments, environment variables, split files or task tables are invalid."""


class RunLocked(ConfigurationError, RuntimeError):
    """Another process holds the run directory's writer lock."""


# -- a unit reported a failure from another process ---------------------------

class UnitFailed(SkillExpandError, RuntimeError):
    """Raised by a stage for a non-retryable unit failure record."""

    def __init__(self, failure: dict):
        super().__init__(f"{failure.get('unit_id')}: {failure.get('type')}: {failure.get('message')}")
        self.failure = failure
        self.category = Category(failure.get('category', Category.BUG.value))


# -- translation of library exceptions ----------------------------------------

Target = Union[Type[SkillExpandError], Callable[[BaseException], Type[SkillExpandError]]]
_TRANSLATIONS: List[Tuple[type, Target, bool]] = []


def register_translation(exc_type: type, target: Target, transient: Optional[bool] = None) -> None:
    """Map a library exception type (and its subclasses) to a taxonomy class.

    ``target`` is a class, or a function of the exception returning one.
    ``transient`` marks types that may be retried as infrastructure failures;
    it defaults from a class target and must be given for a function.
    """
    if isinstance(target, type):
        if not issubclass(target, SkillExpandError):
            raise TypeError('translations must target a SkillExpandError subclass')
        if transient is None:
            transient = issubclass(target, InfrastructureError)
    elif transient is None:
        raise TypeError('declare transient=True/False for a function translation')
    _TRANSLATIONS[:] = [entry for entry in _TRANSLATIONS if entry[0] is not exc_type]
    _TRANSLATIONS.append((exc_type, target, transient))


def transient_type_names() -> frozenset:
    """Names of registered library exceptions that may be retried transiently."""
    return frozenset(source.__name__ for source, _, transient in _TRANSLATIONS if transient)


def translate(exc: BaseException) -> Optional[SkillExpandError]:
    """Return ``exc`` itself, a translated instance chained to it, or ``None``."""
    if isinstance(exc, SkillExpandError):
        return exc
    for cls in type(exc).__mro__:
        for source, target, _ in _TRANSLATIONS:
            if cls is source:
                kind = target if isinstance(target, type) else target(exc)
                translated = kind(f'{type(exc).__name__}: {exc}')
                translated.__cause__ = exc
                return translated
    return None


def classify(exc: BaseException) -> Category:
    """Category of ``exc``, looking through explicit ``raise ... from`` causes."""
    current: Optional[BaseException] = exc
    while current is not None:
        translated = translate(current)
        if translated is not None:
            return translated.category
        current = current.__cause__
    return Category.BUG


def disposition(exc: BaseException) -> Disposition:
    return DISPOSITIONS[classify(exc)]
