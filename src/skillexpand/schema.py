"""Immutable records for the Skill Evolution pipeline.

Design rules, all of which exist to make the paper's three claims checkable
after the fact:

1.  **Append-only and hash-addressable.**  Every record is a frozen dataclass
    with a deterministic id derived from its content, never from wall-clock
    time.  Re-running the same inputs yields the same ids, so a partial run can
    be resumed and diffed.

2.  **Raw per-task outcomes are stored, not just verdicts.**  The val
    gate's thresholds are a *post-hoc* analysis choice; storing only
    ``admitted: bool`` would force a full re-run to try a different threshold.
    Every evaluated task keeps its own outcome.

3.  **Comparisons are paired.**  A candidate is only ever compared against the
    skill it replaces on the *same* task ids.  ``PairedDelta`` keeps the per-task
    ``pairs`` alongside the mean, because a mean shift alone cannot
    distinguish "better everywhere" from "better on two tasks, worse on two".

4.  **Isolation is recorded, not assumed.**  Whether the executor was fresh and
    whether train experience was withheld are explicit fields, and
    :func:`assert_isolation_valid` fails loudly when a configuration that
    claims to test consolidation still leaks experience.

This module deliberately depends on nothing but the standard library, so the
methodology can be unit-tested without the ALFWorld stack or an LLM.
"""

# NOTE: deliberately NO `from __future__ import annotations` here.
# That import turns every annotation into a string, so `dataclasses.fields(x)[i].type`
# stops being a real type object -- which would silently break from_dict's
# type-driven reconstruction. Every annotation below is a runtime-valid 3.9
# construct from `typing`, so the future import buys nothing and costs correctness.

import hashlib
import json
import sys
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 3

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

#: Where a task sits in the three-way split of a family.
#:
#: ``TRAIN`` tasks may produce task experience that drives skill edits.
#: ``VAL`` tasks may be evaluated repeatedly while accepting or rejecting
#: candidates, so scores on them are contaminated by selection.
#: ``TEST`` tasks must never enter the evolution loop; they carry the only
#: uncontaminated estimate of generalisation.
SPLIT_TRAIN = 'train'
SPLIT_VAL = 'val'
SPLIT_TEST = 'test'
SPLITS = (SPLIT_TRAIN, SPLIT_VAL, SPLIT_TEST)

ROLE_EVAL = 'eval'

MODE_CONSOLIDATED_DIRECT = 'consolidated_direct'       # A_fresh(x; S')

ARM_BASE = 'base'
ARM_CANDIDATE = 'candidate'

SELECTION_AGENT = 'agent'
SELECTION_FIXED = 'fixed'
SELECTION_UNSKILLED = 'unskilled'
SELECTIONS = (SELECTION_AGENT, SELECTION_FIXED, SELECTION_UNSKILLED)


def reason_tuple(value: Any, owner: str = 'record') -> Tuple[str, ...]:
    """Coerce a ``reasons`` field to a tuple of strings, refusing a bare string.

    A bare string is the one mistake this exists to catch.  ``reasons=('a ' 'b')``
    is a *string*, not a one-element tuple -- the parentheses do nothing without a
    trailing comma -- and ``tuple()`` of it silently produces one entry per
    character.  The field then looks populated and reads as gibberish, which is how
    a gate decision came to be recorded as a 120-character sequence of single
    letters without anything raising.

    ``None`` is accepted and becomes the empty tuple, because "no reasons" is a
    legitimate state (a pass) rather than a mistake.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        raise TypeError(
            f'{owner}.reasons is a single string, not a sequence of strings: '
            f'{value[:60]!r}. Wrap it in a tuple -- add a trailing comma. '
            'Passing a bare string here silently becomes one entry per character.')
    return tuple(str(item) for item in value)


def _canonical_json(payload: Any) -> str:
    """Stable JSON for hashing: sorted keys, no incidental whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)


def content_hash(payload: Any) -> str:
    """Short deterministic id fragment for a JSON-serialisable payload."""
    return hashlib.sha256(_canonical_json(payload).encode('utf-8')).hexdigest()[:12]


# --------------------------------------------------------------------------
# Generic (de)serialisation
# --------------------------------------------------------------------------
# Hand-writing from_dict for a dozen nested records invites exactly the kind of
# silent field-dropping that makes logged evidence untrustworthy. Instead we
# drive (de)serialisation off the dataclass field types and round-trip-test it.

def _strip_optional(tp: Any) -> Any:
    """Unwrap Optional[X] / Union[X, None] to X."""
    origin = getattr(tp, '__origin__', None)
    if origin is None:
        return tp
    args = [a for a in getattr(tp, '__args__', ()) if a is not type(None)]
    # Union of exactly one non-None type is what Optional[X] produces.
    if len(args) == 1:
        return args[0]
    return tp


def _resolve_forward(tp: Any, owner: Any) -> Any:
    """Resolve a string annotation against the module the record lives in.

    Needed because a hand-written forward reference -- ``Tuple['ArmEvaluation', ...]``
    -- reaches :func:`from_dict` as the *string* ``'ArmEvaluation'``.  Without this,
    the nested records silently deserialise to plain dicts: the outer record
    round-trips, the inner one does not, and the damage only surfaces much later as
    an ``AttributeError`` inside an analysis script.  Raising is the point of this
    function; an unresolvable annotation must be loud, not a dict.
    """
    if not isinstance(tp, str):
        return tp
    module = sys.modules.get(getattr(owner, '__module__', None), None)
    try:
        return eval(tp, dict(getattr(module, '__dict__', {})))  # noqa: S307
    except Exception as exc:  # noqa: BLE001
        raise TypeError(
            f'cannot resolve forward reference {tp!r} declared by '
            f'{getattr(owner, "__name__", owner)!r}; annotate the field with the '
            'real type, or make the name importable in its module') from exc


def to_dict(obj: Any) -> Any:
    """Recursively convert dataclasses / tuples into JSON-safe containers."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_dict(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [to_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): to_dict(v) for k, v in obj.items()}
    return obj


def from_dict(tp: Any, data: Any) -> Any:
    """Inverse of :func:`to_dict`, driven by the declared field types."""
    if data is None:
        return None

    if isinstance(tp, str):
        raise TypeError(f'unresolved forward reference {tp!r} in a record type')

    tp = _strip_optional(tp)
    origin = getattr(tp, '__origin__', None)

    # Tuple[X, ...] / List[X]
    if origin in (tuple, list) or isinstance(tp, type) and issubclass(tp, (tuple, list)):
        args = getattr(tp, '__args__', ())
        if not args:
            return tuple(data)
        # Tuple[X, ...] declares args == (X, Ellipsis)
        elem = args[0] if len(args) == 1 or args[-1] is Ellipsis else None
        if elem is None:
            return tuple(data)
        return tuple(from_dict(_resolve_forward(elem, tp), x) for x in data)

    # Dict[str, X]
    if origin is dict:
        args = getattr(tp, '__args__', ())
        val_tp = args[1] if len(args) == 2 else Any
        return {k: from_dict(_resolve_forward(val_tp, tp), v) for k, v in data.items()}

    if is_dataclass(tp):
        kwargs = {}
        for f in fields(tp):
            if f.name in data:
                kwargs[f.name] = from_dict(_resolve_forward(f.type, tp), data[f.name])
        return tp(**kwargs)

    return data


def to_jsonl(obj: Any) -> str:
    """One record per line, keys sorted so diffs stay readable."""
    return _canonical_json(to_dict(obj))


# --------------------------------------------------------------------------
# Knowledge artefacts
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Provenance:
    """Why an artefact exists and what it was derived from.

    Kept explicit because the whole paper hinges on the difference between
    "this text was summarised from experience" and "this text survived a
    generalisation test".
    """

    rationale: str = ''
    source_experience_ids: Tuple[str, ...] = ()
    source_task_ids: Tuple[int, ...] = ()
    episode_id: Optional[str] = None

    def __post_init__(self) -> None:
        # Accept lists from JSON and normalise to tuples so records stay hashable.
        object.__setattr__(self, 'source_experience_ids', tuple(self.source_experience_ids))
        object.__setattr__(self, 'source_task_ids', tuple(self.source_task_ids))


@dataclass(frozen=True)
class Skill:
    """A promoted, reusable knowledge artefact.

    ``body`` is the *only* field an executor ever sees.  ``name`` and
    ``description`` exist for retrieval, ``family_id`` for evaluation-reference
    mapping.  Keeping that separation explicit is what lets the paper test
    routing independently of skill quality.
    """

    skill_id: str
    family_id: str
    version: int
    name: str
    description: str
    body: str
    provenance: Provenance = field(default_factory=Provenance)

    def __post_init__(self):
        if not isinstance(self.description,str) or not self.description.strip():
            raise ValueError('Skill description is required for routing')

    @property
    def key(self) -> str:
        return f'{self.skill_id}@v{self.version}'

    def to_dict(self) -> Dict[str, Any]:
        d = to_dict(self)
        d['key'] = self.key
        return d


@dataclass(frozen=True)
class SkillEdit:
    """One structured edit applied to a candidate: the real section and stable
    target rule ID used to construct it, kept for audit replay."""

    op: str
    text: str
    rule_index: Optional[int] = None
    section: Optional[str] = None
    target_id: Optional[str] = None

    def __post_init__(self) -> None:
        allowed = ('ADD', 'EDIT', 'REMOVE', 'AGREE')
        if self.op.upper() not in allowed:
            raise ValueError(f'unknown edit op {self.op!r}, expected one of {allowed}')
        object.__setattr__(self, 'op', self.op.upper())


@dataclass(frozen=True)
class CandidateSkill:
    """A *proposal*.  Never written into the live library by construction;
    only an accepted batch commits its ``skill``."""

    candidate_id: str
    base_skill_key: str
    skill: Skill
    raw_llm_output: str
    proposed_from_experience_id: str
    edits: Tuple[SkillEdit, ...] = ()

    def __post_init__(self) -> None:
        if self.skill.key == self.base_skill_key:
            raise ValueError(
                'candidate is identical to its base skill; a proposal must '
                'either change the body or be rejected before reaching here')

@dataclass(frozen=True)
class Claim:
    """The falsifiable part of a proposal: what the edit claims to do.

    A rationale is unverifiable prose, and an unverifiable statement cannot be
    held against the party who made it -- which is exactly what a reviewer needs
    to be able to do.  The claim splits the rationale into the two things a third
    party *can* check against a pair of execution traces: the situation in which
    the new rule is supposed to fire, and the action it is supposed to change.
    Both are single-line and bounded, so what gets verified is an observable
    condition rather than an argument.

    A claim describes one *proposal*, never the Skill: only the accepted body
    enters the library, so this record lives in the batch journal.
    """

    trigger: str
    action_change: str

    #: Long enough for a conditional sentence, short enough to stay checkable.
    MAX_CHARS = 400

    def __post_init__(self) -> None:
        for name in ('trigger', 'action_change'):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f'claim {name} is required')
            value = value.strip()
            if '\n' in value or '\r' in value:
                raise ValueError(f'claim {name} must occupy one line')
            if len(value) > self.MAX_CHARS:
                raise ValueError(
                    f'claim {name} exceeds {self.MAX_CHARS} characters')
            object.__setattr__(self, name, value)
        if self.trigger == self.action_change:
            raise ValueError('claim trigger and action_change must differ')

    @property
    def claim_id(self) -> str:
        return content_hash(self.payload())

    def payload(self) -> Dict[str, str]:
        return {'trigger': self.trigger, 'action_change': self.action_change}

    def render(self) -> str:
        return f'trigger: {self.trigger}\naction change: {self.action_change}'

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.payload(), claim_id=self.claim_id)


# --------------------------------------------------------------------------
# Task-level experience
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TaskExperience:
    """Layer 1: what happened inside one task, before any consolidation.

    Deliberately *not* a skill. It is raw, task-local, and its persisted id is
    namespaced by the cold-start/evolution round so repeated Skill-aware runs
    remain distinct and auditable.
    """

    experience_id: str
    benchmark: str
    task_id: int
    task: str
    family_id: str
    split: str
    reward: bool
    num_trials: int
    initial_skill_key: Optional[str] = None
    failed_trajectories: Tuple[str, ...] = ()
    reflections: Tuple[str, ...] = ()
    final_trajectory: Optional[str] = None
    selected_skill_id: Optional[str] = None
    selection_source: str = SELECTION_FIXED
    #: How the answer was obtained: a model choice, or which fallback fired.
    #:
    #: Separate from ``selection_source``, which names the *mode*.  Storing only the
    #: raw answer made an agent-mode fallback indistinguishable from a confident
    #: choice once the run was over -- the raw text is a model answer only in the
    #: first case -- and the first real run reported every selection as a model
    #: answer even in oracle mode.
    selection_reason: str = ''
    selection_raw: str = ''
    #: Reward of each repair-loop trial in order.  ``reward`` is the disjunction.
    #:
    #: Kept because "solved on trial 0" and "solved on trial 3" are different
    #: pieces of evidence about the skill: the first says the skill was sufficient,
    #: the second says reflection carried the task and the skill was not.  Collapsing
    #: them into one boolean is how a skill edit gets credited for work reflection
    #: did.
    trial_rewards: Tuple[bool, ...] = ()
    trial_phases: Tuple[str, ...] = ()
    experience_card: Optional[Dict[str, Any]] = None
    l1_audit_path: Optional[str] = None
    l1_trials: Tuple[Dict[str, Any], ...] = ()
    # 0 is the skill-free cold start; positive values identify later Skill-aware
    # L1 rounds.  Keeping this on the record makes multi-round evidence immutable
    # and prevents cards from different Skill revisions being mixed accidentally.
    evolution_round: int = 0

    @property
    def autonomous_solved(self) -> bool:
        return any(ok for ok, phase in zip(self.trial_rewards, self.trial_phases)
                   if phase == 'autonomous') if self.trial_phases else self.reward

    @property
    def supervised_repaired(self) -> bool:
        return self.reward and not self.autonomous_solved and 'supervised' in self.trial_phases


    def __post_init__(self) -> None:
        if self.split not in SPLITS:
            raise ValueError(f'unknown split {self.split!r}, expected one of {SPLITS}')
        if self.selection_source not in SELECTIONS:
            raise ValueError(
                f'unknown selection_source {self.selection_source!r}, '
                f'expected one of {SELECTIONS}')
        object.__setattr__(self, 'failed_trajectories', tuple(self.failed_trajectories))
        object.__setattr__(self, 'reflections', tuple(self.reflections))
        object.__setattr__(self, 'trial_rewards', tuple(self.trial_rewards))

    @staticmethod
    def make_id(benchmark: str, family_id: str, task_id: int) -> str:
        return f'{benchmark}:{family_id}:{task_id}'


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TaskOutcome:
    """One task executed once, under one arm.

    ``repeat`` exists because the executor is not reliably deterministic.  The
    served model is reached through vLLM, whose reduction order changes with batch
    composition and chunked-prefill boundaries; measured directly, the same prompt
    at ~8k tokens returned two different continuations across three identical
    requests even with temperature 0.  A single run is therefore a sample, not a
    measurement, and every arm records which repetition an outcome came from.
    """

    task_id: int
    family_id: str
    role: str
    success: bool
    num_steps: int = 0
    truncated: bool = False
    repeat: int = 0
    #: Free-form per-run note (e.g. a failure-mode label).
    note: str = ''


@dataclass(frozen=True)
class ArmEvaluation:
    """One skill revision measured over one task set, under a declared isolation
    configuration.

    The three isolation flags are load-bearing.  Upstream ExpeL's eval does not
    set them the way its prose implies: ``update_dynamic_prompt_components``
    (``agent/expel.py:572``) rebuilds the retrieval pool from
    ``succeeded_trial_history``, so with the default
    ``fewshot_strategy: task_similarity`` the executor receives full successful
    training trajectories of the same family in addition to the rules.  An arm
    that claims to test knowledge consolidation must record
    ``experience_withheld=True`` and ``fewshot_strategy='none'``.
    """

    arm_id: str
    role: str
    mode: str
    skill_key: Optional[str]
    outcomes: Tuple[TaskOutcome, ...]
    executor_fresh: bool
    experience_withheld: bool
    fewshot_strategy: str

    def __post_init__(self) -> None:
        if self.arm_id not in (ARM_BASE, ARM_CANDIDATE):
            raise ValueError(f'unknown arm_id {self.arm_id!r}')
        object.__setattr__(self, 'outcomes', tuple(self.outcomes))

    @property
    def successes(self) -> int:
        return sum(1 for o in self.outcomes if o.success)

    @property
    def n(self) -> int:
        return len(self.outcomes)

    def by_task_rate(self) -> Dict[int, float]:
        """Per-task success rate, averaged over repeats.

        With repeats this is the quantity a comparison uses: pairing raw booleans
        would silently keep one arbitrary repetition per task and throw the rest
        away, which is exactly how an intermittent flake becomes a finding.
        """
        acc: Dict[int, List[float]] = {}
        for outcome in self.outcomes:
            acc.setdefault(outcome.task_id, []).append(1.0 if outcome.success else 0.0)
        return {task_id: sum(vals) / len(vals) for task_id, vals in acc.items()}


def assert_isolation_valid(arms: Sequence[ArmEvaluation], owner: str,
                           require_isolation: bool = True) -> None:
    """Fail loudly if an arm that claims consolidation still leaks experience.

    Called before a measurement is written, so a misconfiguration is caught at the
    point of measurement rather than discovered during analysis.
    """
    if not require_isolation:
        return
    for arm in arms:
        if arm.mode != MODE_CONSOLIDATED_DIRECT:
            continue
        if not arm.executor_fresh:
            raise AssertionError(
                f'{owner}: consolidated-direct arm {arm.arm_id}/{arm.role} '
                'reused an executor instance; a reset() is not isolation')
        if not arm.experience_withheld:
            raise AssertionError(
                f'{owner}: consolidated-direct arm {arm.arm_id}/{arm.role} '
                'left source experience visible')
        if arm.fewshot_strategy != 'none':
            raise AssertionError(
                f'{owner}: consolidated-direct arm {arm.arm_id}/{arm.role} '
                f"used fewshot_strategy={arm.fewshot_strategy!r}; ExpeL's retrieval "
                'pool is built from succeeded_trial_history (agent/expel.py:572), so '
                'anything other than "none" injects raw training trajectories')


# --------------------------------------------------------------------------
# Paired comparison
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PairedDelta:
    n_paired: int
    mean_delta: Optional[float]
    pairs: Tuple[Tuple[int, float, float], ...] = ()


def paired_delta(base: ArmEvaluation, candidate: ArmEvaluation) -> PairedDelta:
    """Pair two arms on shared task ids and summarise the per-task difference."""
    if base.role != candidate.role:
        raise ValueError(
            f'cannot pair roles {base.role!r} and {candidate.role!r}: '
            'a paired comparison must hold the task role fixed')

    b, c = base.by_task_rate(), candidate.by_task_rate()
    pairs = tuple((tid, b[tid], c[tid]) for tid in sorted(set(b) & set(c)))
    deltas = [cs - bs for _, bs, cs in pairs]
    return PairedDelta(
        n_paired=len(pairs),
        mean_delta=(sum(deltas) / len(deltas)) if deltas else None,
        pairs=pairs,
    )


# --------------------------------------------------------------------------
# Validation results
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationResult:

    skill_id: str
    panel_key: str
    task_ids: Tuple[int, ...]
    base_skill_key: str
    candidate_skill_key: str
    arms: Tuple[ArmEvaluation, ...]
    metrics: Dict[str, Optional[float]]
    passed: bool
    reasons: Tuple[str, ...] = ()
    #: Per-task success rates under each arm, as ``(task_id, base, candidate)``.
    pairs: Tuple[Tuple[int, float, float], ...] = ()
    #: Raw per-task reviewer probabilities and reasons.  The ``pairs`` field
    #: intentionally stays compact and boolean-like; this field keeps the
    #: unit-level evidence so the analysis can be redone without calling the
    #: reviewer again.
    prediction_rows: Tuple[Dict[str, Any], ...] = ()
    returned_to_editor: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, 'task_ids', tuple(self.task_ids))
        object.__setattr__(self, 'arms', tuple(self.arms))
        object.__setattr__(self, 'pairs', tuple(self.pairs))
        object.__setattr__(self, 'prediction_rows', tuple(self.prediction_rows))
        object.__setattr__(self, 'reasons', reason_tuple(self.reasons,
                                                        'ValidationResult'))

    @property
    def score_before(self) -> Optional[float]:
        return self.metrics.get('mean_base')

    @property
    def score_after(self) -> Optional[float]:
        return self.metrics.get('mean_candidate')

    @property
    def delta(self) -> Optional[float]:
        return self.metrics.get('success_delta')


# --------------------------------------------------------------------------
# Data split
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SplitPlan:
    """Per-family three-way split.

    ``test`` tasks must never appear in a :class:`TaskExperience`.  Without
    that discipline every candidate edit is indirectly fitted to the same
    held-out set that the paper reports on.
    """

    benchmark: str
    seed: int
    #: task_id -> one of SPLITS
    assignment: Dict[int, str]
    #: family_id -> task_id list, for readability in logs
    families: Dict[str, Tuple[int, ...]] = field(default_factory=dict)

    def split_of(self, task_id: int) -> str:
        try:
            return self.assignment[task_id]
        except KeyError:
            raise KeyError(f'task {task_id} is not covered by the split plan') from None

    def tasks_in(self, split: str) -> Tuple[int, ...]:
        if split not in SPLITS:
            raise ValueError(f'unknown split {split!r}')
        return tuple(sorted(t for t, s in self.assignment.items() if s == split))

    def family_of(self, task_id: int) -> Optional[str]:
        for fam, tids in self.families.items():
            if task_id in tids:
                return fam
        return None

    def assert_test_is_untouched(self, experiences: List[TaskExperience]) -> None:
        """Guard the single most expensive mistake in this design."""
        for exp in experiences:
            declared = self.assignment.get(exp.task_id)
            if declared == SPLIT_TEST:
                raise AssertionError(
                    f'experience {exp.experience_id} was collected on test split '
                    f'task {exp.task_id}; test tasks must never enter the '
                    'evolution loop')

    @staticmethod
    def make(assignment: Dict[int, str], benchmark: str, seed: int,
             families: Dict[str, Tuple[int, ...]] = None) -> 'SplitPlan':
        bad = {t: s for t, s in assignment.items() if s not in SPLITS}
        if bad:
            raise ValueError(f'invalid split values: {bad}')
        return SplitPlan(
            benchmark=benchmark,
            seed=seed,
            assignment=dict(assignment),
            families={k: tuple(v) for k, v in (families or {}).items()},
        )


