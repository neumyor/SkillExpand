"""Immutable records for the Skill Evolution pipeline.

Design rules, all of which exist to make the paper's three claims checkable
after the fact:

1.  **Append-only and hash-addressable.**  Every record is a frozen dataclass
    with a deterministic id derived from its content, never from wall-clock
    time.  Re-running the same inputs yields the same ids, so a partial run can
    be resumed and diffed.

2.  **Raw per-task outcomes are stored, not just verdicts.**  The admission
    gate's thresholds are a *post-hoc* analysis choice; storing only
    ``admitted: bool`` would force a full re-run to try a different threshold.
    Every evaluated task keeps its own outcome.

3.  **Comparisons are paired.**  A candidate is only ever compared against the
    skill it replaces on the *same* task ids.  ``PairedDelta`` reports wins /
    losses / ties alongside the mean, because a mean shift alone cannot
    distinguish "better everywhere" from "better on two tasks, worse on two".

4.  **Isolation is recorded, not assumed.**  Whether the executor was fresh and
    whether source experience was withheld are explicit fields, and
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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 3

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

#: Where a task sits in the three-way split of a family.
#:
#: ``SOURCE`` tasks may produce task experience that drives skill edits.
#: ``ADMISSION`` tasks may be evaluated repeatedly while accepting or rejecting
#: candidates, so scores on them are contaminated by selection.
#: ``FINAL`` tasks must never enter the evolution loop; they carry the only
#: uncontaminated estimate of generalisation.
SPLIT_SOURCE = 'source'
SPLIT_ADMISSION = 'admission'
SPLIT_FINAL = 'final'
SPLITS = (SPLIT_SOURCE, SPLIT_ADMISSION, SPLIT_FINAL)

ROLE_EVAL = 'eval'
ROLES = (ROLE_EVAL,)

#: The four evaluation conditions from the plan.
MODE_VANILLA = 'vanilla'                              # A(x)
MODE_TASK_ADAPTED = 'task_adapted'                     # A(x; S, E_x)
MODE_CONSOLIDATED_DIRECT = 'consolidated_direct'       # A_fresh(x; S')
MODE_CONSOLIDATED_RETRIEVED = 'consolidated_retrieved' # A_fresh(x; Retrieve(x, S))
MODES = (MODE_VANILLA, MODE_TASK_ADAPTED,
         MODE_CONSOLIDATED_DIRECT, MODE_CONSOLIDATED_RETRIEVED)

ARM_BASE = 'base'
ARM_CANDIDATE = 'candidate'
#: A measurement of one revision with no pairing implied, used by the validation panel.
#:
#: The panel scores a revision on its own, and the base/candidate labels are attached
#: later when a score is turned into an arm of the comparison.  Labelling a panel run as
#: "the candidate arm" because it happens to be the one being measured would be a claim
#: about a pairing that does not exist yet at that point.
ARM_EVAL = 'eval'
ARMS = (ARM_BASE, ARM_CANDIDATE, ARM_EVAL)

SELECTION_AGENT = 'agent'
SELECTION_FIXED = 'fixed'
SELECTION_UNSKILLED = 'unskilled'
SELECTIONS = (SELECTION_AGENT, SELECTION_FIXED, SELECTION_UNSKILLED)

UPDATE_ACCEPTED = 'accepted'
#: The candidate was measured and did not strictly beat the stable head.  A tie is a
#: rejection: ``vc > vs`` is the whole rule, and a candidate that merely matches the
#: head has not been shown to be an improvement that would justify invalidating the
#: pool and the reject buffer.
UPDATE_REJECTED = 'rejected'
#: The patch was byte-identical (after canonicalisation) to one already rejected
#: against this same stable head.  Rejected without spending a validation run, because
#: the outcome is already known -- see :func:`patch_hash`.
UPDATE_DUPLICATE_PATCH = 'duplicate_patch'
UPDATE_NO_PROPOSAL = 'no_proposal'
UPDATE_NO_POOLED_TASKS = 'no_pooled_tasks'
UPDATE_OUTCOMES = (UPDATE_ACCEPTED, UPDATE_REJECTED, UPDATE_DUPLICATE_PATCH,
                   UPDATE_NO_PROPOSAL, UPDATE_NO_POOLED_TASKS)

OUTCOME_DIRECT_SUCCESS = 'direct_success'
OUTCOME_REFLECTION_RECOVERED = 'reflection_recovered'
OUTCOME_HARD_FAILURE = 'hard_failure'
OUTCOME_TYPES = (OUTCOME_DIRECT_SUCCESS, OUTCOME_REFLECTION_RECOVERED,
                 OUTCOME_HARD_FAILURE)

TRIGGER_POSITIVE = 'positive'
TRIGGER_NEGATIVE = 'negative'
TRIGGER_MIXED = 'mixed'
TRIGGER_NO_REPAIR_SIGNAL = 'no_repair_signal'
LEARNING_TRIGGERS = (TRIGGER_POSITIVE, TRIGGER_NEGATIVE, TRIGGER_MIXED, TRIGGER_NO_REPAIR_SIGNAL)

SELECTION_ATTEMPT_SUCCESS = 'success'
SELECTION_ATTEMPT_FAILED = 'failed'
SELECTION_ATTEMPT_STATUSES = (SELECTION_ATTEMPT_SUCCESS, SELECTION_ATTEMPT_FAILED)

#: Pool threshold triggers L2 candidate generation.
TRIGGER_POOL_THRESHOLD = 'pool_threshold'
TRIGGER_FIXED_BATCH = 'fixed_batch'
TRIGGERS = (TRIGGER_POOL_THRESHOLD, TRIGGER_FIXED_BATCH)

VERDICT_ACCEPT = 'accept'
VERDICT_REJECT = 'reject'
VERDICTS = (VERDICT_ACCEPT, VERDICT_REJECT)

META_ACTION_SYNTHESISE = 'synthesise'
META_ACTION_HOLD = 'hold'
META_ACTIONS = (META_ACTION_SYNTHESISE, META_ACTION_HOLD)


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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


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


def from_jsonl(cls: Any, line: str) -> Any:
    return from_dict(cls, json.loads(line))


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

    created_at: str = field(default_factory=utc_now)
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
    """One parsed operation proposed by the Attributor.

    Mirrors ExpeL's ``ADD / EDIT / REMOVE / AGREE`` vocabulary
    (``agent/expel.py:665`` ``parse_rules``).  Storing the operations, not just
    the resulting text, is what lets the meta-layer learn *which kinds of edit*
    survive admission.
    """

    op: str
    text: str
    rule_index: Optional[int] = None

    def __post_init__(self) -> None:
        allowed = ('ADD', 'EDIT', 'REMOVE', 'AGREE')
        if self.op.upper() not in allowed:
            raise ValueError(f'unknown edit op {self.op!r}, expected one of {allowed}')
        object.__setattr__(self, 'op', self.op.upper())


@dataclass(frozen=True)
class CandidateSkill:
    """A *proposal*.  Never written into the live library by construction.

    ExpeL conflates proposing with committing -- ``create_rules`` mutates
    ``self.rule_items_with_count`` in place (``agent/expel.py:303-317``) -- so
    downstream code must treat this object as the only carrier of a proposal.
    """

    candidate_id: str
    base_skill_key: str
    skill: Skill
    raw_llm_output: str
    meta_skill_version: int
    proposed_from_experience_id: str
    edits: Tuple[SkillEdit, ...] = ()

    def __post_init__(self) -> None:
        if self.skill.key == self.base_skill_key:
            raise ValueError(
                'candidate is identical to its base skill; a proposal must '
                'either change the body or be rejected before reaching here')

    @property
    def differs_from_base(self) -> bool:
        return True


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
    initial_meta_skill_version: Optional[int] = None
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

    @property
    def is_source_eligible(self) -> bool:
        """Only SOURCE-split tasks may drive a skill edit."""
        return self.split == SPLIT_SOURCE

    @property
    def solved_on_first_trial(self) -> bool:
        """True when trial 0 already succeeded, i.e. without any reflection."""
        return bool(self.trial_rewards) and self.trial_rewards[0]

    @property
    def reflections_used(self) -> int:
        """How many trials were needed beyond a first-try success."""
        if not self.trial_rewards:
            return 0
        for i, ok in enumerate(self.trial_rewards):
            if ok:
                return i
        return len(self.trial_rewards) - 1

    @property
    def outcome_type(self) -> str:
        if self.experience_card is not None:
            return (OUTCOME_DIRECT_SUCCESS if self.solved_on_first_trial else
                    OUTCOME_REFLECTION_RECOVERED if self.reward else OUTCOME_HARD_FAILURE)
        if not self.trial_rewards:
            return (OUTCOME_DIRECT_SUCCESS if self.reward else OUTCOME_HARD_FAILURE)
        if self.trial_rewards[0]:
            return OUTCOME_DIRECT_SUCCESS
        if any(self.trial_rewards):
            return OUTCOME_REFLECTION_RECOVERED
        return OUTCOME_HARD_FAILURE

    @property
    def is_learning_signal(self) -> bool:
        """Whether this experience is allowed to *trigger* a skill update.

        Only ``reflection_recovered``.  A ``direct_success`` says the skill already
        works and serves as a regression anchor; a ``hard_failure`` says the skill is
        inadequate but not how to fix it -- measured, every round that tried to repair
        a hard failure in the pool failed the strict rule, because the pool's failures
        are mostly outside the executor's reach.  Triggering on those makes the loop
        spend its whole budget on unsolvable targets.
        """
        return self.outcome_type == OUTCOME_REFLECTION_RECOVERED

    @property
    def fell_back(self) -> bool:
        return False


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

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f'unknown role {self.role!r}, expected one of {ROLES}')


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
        if self.role not in ROLES:
            raise ValueError(f'unknown role {self.role!r}')
        if self.mode not in MODES:
            raise ValueError(f'unknown mode {self.mode!r}')
        object.__setattr__(self, 'outcomes', tuple(self.outcomes))

    @property
    def successes(self) -> int:
        return sum(1 for o in self.outcomes if o.success)

    @property
    def n(self) -> int:
        return len(self.outcomes)

    @property
    def success_rate(self) -> Optional[float]:
        """``None`` when no task was evaluated -- never silently 0.0.

        A missing measurement and a measured failure must not be conflated;
        upstream analysis that treats them alike is how a 0/0 becomes a 0%.
        """
        return (self.successes / self.n) if self.n else None

    def by_task(self) -> Dict[int, TaskOutcome]:
        """Last outcome per task id.  Only meaningful when ``repeats == 1``."""
        return {o.task_id: o for o in self.outcomes}

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

    @property
    def repeats(self) -> int:
        counts: Dict[int, int] = {}
        for outcome in self.outcomes:
            counts[outcome.task_id] = counts.get(outcome.task_id, 0) + 1
        return max(counts.values()) if counts else 0


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

    role: str
    n_paired: int
    n_only_base: int
    n_only_candidate: int
    mean_delta: Optional[float]
    mean_base: Optional[float]
    mean_candidate: Optional[float]
    wins: int
    losses: int
    ties: int
    pairs: Tuple[Tuple[int, float, float], ...] = ()
    #: Differences within this magnitude count as a tie.
    tie_tolerance: float = 1e-9

    @property
    def is_informative(self) -> bool:
        return self.n_paired > 0


def paired_delta(base: ArmEvaluation, candidate: ArmEvaluation,
                 tie_tolerance: float = 1e-9) -> PairedDelta:
    """Pair two arms on shared task ids and summarise the per-task difference."""
    if base.role != candidate.role:
        raise ValueError(
            f'cannot pair roles {base.role!r} and {candidate.role!r}: '
            'a paired comparison must hold the task role fixed')

    b, c = base.by_task_rate(), candidate.by_task_rate()
    shared = sorted(set(b) & set(c))
    pairs = tuple((tid, b[tid], c[tid]) for tid in shared)
    deltas = [cs - bs for _, bs, cs in pairs]

    return PairedDelta(
        role=base.role,
        n_paired=len(shared),
        n_only_base=len(set(b) - set(c)),
        n_only_candidate=len(set(c) - set(b)),
        mean_delta=(sum(deltas) / len(deltas)) if deltas else None,
        mean_base=(sum(bs for _, bs, _ in pairs) / len(pairs)) if pairs else None,
        mean_candidate=(sum(cs for _, _, cs in pairs) / len(pairs)) if pairs else None,
        wins=sum(1 for d in deltas if d > tie_tolerance),
        losses=sum(1 for d in deltas if d < -tie_tolerance),
        ties=sum(1 for d in deltas if abs(d) <= tie_tolerance),
        pairs=pairs,
        tie_tolerance=tie_tolerance,
    )


# --------------------------------------------------------------------------
# The skill selector and the meta layer
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SelectionAttempt:

    attempt_id: str
    benchmark: str
    task_id: int
    family_id: str
    attempt_index: int
    status: str
    reason: str
    raw_output: str = ''
    skill_id: str = ''
    mode: str = SELECTION_AGENT
    prompt_chars: int = 0
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.status not in SELECTION_ATTEMPT_STATUSES:
            raise ValueError(f'unknown selection status {self.status!r}')
        if self.status == SELECTION_ATTEMPT_SUCCESS and not self.skill_id:
            raise ValueError('a successful selection attempt must name the skill')

    @property
    def succeeded(self) -> bool:
        return self.status == SELECTION_ATTEMPT_SUCCESS


@dataclass(frozen=True)
class PatchAttemptRecord:

    patch_id: str
    stable_head_key: str
    #: Canonicalised hash of the patch body; the duplicate guard compares this.
    patch_hash: str
    #: The resulting skill text the patch proposed, verbatim.
    candidate_body: str
    validation_before: float
    validation_after: float
    #: 'accept' / 'reject'.  Only 'reject' entries live in a reject buffer.
    verdict: str
    reasons: Tuple[str, ...] = ()
    created_at: str = field(default_factory=utc_now)

    candidate_description: str = ''

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ValueError(f'unknown patch verdict {self.verdict!r}')
        object.__setattr__(self, 'reasons', reason_tuple(self.reasons,
                                                        'PatchAttemptRecord'))

    @property
    def delta(self) -> float:
        return self.validation_after - self.validation_before


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
    returned_to_editor: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, 'task_ids', tuple(self.task_ids))
        object.__setattr__(self, 'arms', tuple(self.arms))
        object.__setattr__(self, 'pairs', tuple(self.pairs))
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

    @property
    def regressed_task_ids(self) -> Tuple[int, ...]:
        tol = 1e-9
        return tuple(t for t, b, c in self.pairs if c < b - tol)

    @property
    def recovered_task_ids(self) -> Tuple[int, ...]:
        tol = 1e-9
        return tuple(t for t, b, c in self.pairs if c > b + tol)

    def arm(self, arm_id: str) -> Optional[ArmEvaluation]:
        for a in self.arms:
            if a.arm_id == arm_id:
                return a
        return None


@dataclass(frozen=True)
class MetaSkill:
    """Layer 3: a natural-language policy describing *how to update skills*.

    It conditions the editor's prompt.  It is never derived from a single failure:
    The retired online L3 updater synthesised it periodically from a
    batch of patch outcomes, which is what makes it slow memory rather than a
    reaction to the last thing that happened.
    """

    version: int
    body: str
    parent_version: Optional[int] = None
    created_at: str = field(default_factory=utc_now)
    derived_from_patch_ids: Tuple[str, ...] = ()
    rationale: str = ''
    trigger: str = 'threshold'

    def __post_init__(self) -> None:
        object.__setattr__(self, 'derived_from_patch_ids',
                           tuple(self.derived_from_patch_ids))

    @property
    def key(self) -> str:
        return f'M@v{self.version}'


# --------------------------------------------------------------------------
# Data split
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SplitPlan:
    """Per-family three-way split.

    ``final`` tasks must never appear in a :class:`TaskExperience`.  Without
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

    def assert_final_is_untouched(self, experiences: List[TaskExperience]) -> None:
        """Guard the single most expensive mistake in this design."""
        for exp in experiences:
            declared = self.assignment.get(exp.task_id)
            if declared == SPLIT_FINAL:
                raise AssertionError(
                    f'experience {exp.experience_id} was collected on FINAL-split '
                    f'task {exp.task_id}; final tasks must never enter the '
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


@dataclass(frozen=True)
class PatchAttempt:

    patch_id: str
    benchmark: str
    skill_id: str
    skill_family_id: str
    stable_head_key: str
    #: The revision the patch would install, i.e. ``head + 1``.  Also the revision the
    #: skill library ultimately received -- identical to ``stable_head_key`` on a
    #: rejection, because a rejection changes nothing.
    candidate_skill_key: str
    #: The editor strategy in force when the patch was proposed.  Frozen at enqueue
    #: time, so a strategy revision that lands while the attempt waits cannot leave
    #: this field describing something that did not happen.
    meta_skill_version: int
    pooled_experience_ids: Tuple[str, ...]
    pooled_task_ids: Tuple[int, ...]
    #: Which held-out panel was measured, and on which tasks.  Stored per attempt even
    #: though the panel is fixed for the whole run: a reader of one record must not have
    #: to consult the configuration to know what the score refers to.
    validation_panel_key: str
    validation_task_ids: Tuple[int, ...]
    #: This attempt's position in its skill's sequence: 0 is the first, and the counter
    #: never resets.  Not decoration -- ``created_at`` has second resolution, so several
    #: attempts of one skill routinely share a timestamp and their log order is otherwise
    #: decided by a content hash.  That makes "which attempt came first" unanswerable
    #: exactly where it matters: the reject buffer is cleared by an archive, and "before or
    #: after the last archive" is a question about order.
    attempt_index: int = 0
    #: The verdict and its supporting numbers.  ``None`` when nothing was measured --
    #: no patch was proposed, or the patch was a duplicate of one already rejected
    #: against this same head.
    validation: Optional[ValidationResult] = None
    #: The patch and its outcome, present whenever a patch was proposed (including a
    #: duplicate, whose record explains the verdict without a measurement).
    #:
    #: ``None`` on an attempt that proposed nothing.  That case is *not* a rejected patch
    #: -- the editor had nothing to say about the batch -- and putting a placeholder
    #: record here would leak into the reject buffer and the duplicate guard, making a
    #: future patch that happens to equal the head's own text look like a repeat offence.
    patch: Optional[PatchAttemptRecord] = None
    outcome: str = UPDATE_NO_PROPOSAL
    #: Why no patch was proposed, when none was.  The editor's own reason, kept because
    #: "answered with AGREEs" and "output did not parse" are failures of different things
    #: and a run that lost the distinction could not tell a strategy problem from a
    #: prompt-formatting problem.
    editor_note: str = ''
    pool_cleared: bool = False
    l1_base_agreement: Optional[float] = None
    trigger: str = TRIGGER_POOL_THRESHOLD
    learning_trigger: str = TRIGGER_POSITIVE
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.outcome not in UPDATE_OUTCOMES:
            raise ValueError(f'unknown patch outcome {self.outcome!r}')
        if self.trigger not in TRIGGERS:
            raise ValueError(f'unknown trigger {self.trigger!r}')
        if self.learning_trigger not in LEARNING_TRIGGERS:
            raise ValueError(f'unknown learning_trigger {self.learning_trigger!r}')
        object.__setattr__(self, 'pooled_experience_ids',
                           tuple(self.pooled_experience_ids))
        object.__setattr__(self, 'pooled_task_ids', tuple(self.pooled_task_ids))
        object.__setattr__(self, 'validation_task_ids',
                           tuple(self.validation_task_ids))
        if self.outcome == UPDATE_ACCEPTED:
            if self.validation is None:
                raise ValueError(
                    'an accepted patch must carry its validation result; without it '
                    'the accept decision is not reproducible from the record')
            if not self.validation.passed:
                raise ValueError(
                    'an accepted patch must have passed its validation; the outcome '
                    'and the verdict disagree')
        if self.outcome == UPDATE_NO_POOLED_TASKS and self.pool_cleared:
            raise ValueError(
                'an attempt with no pooled evidence cannot have cleared its pool; the '
                'archive flag names a pool that existed')

    @property
    def accepted(self) -> bool:
        return self.outcome == UPDATE_ACCEPTED

    @property
    def rejected(self) -> bool:
        return self.outcome in (UPDATE_REJECTED, UPDATE_DUPLICATE_PATCH)

    @property
    def measured(self) -> bool:
        """Whether a validation run actually happened.

        Distinct from "rejected": a duplicate patch is rejected without being measured,
        because its outcome was already measured against the same head.  Anything
        averaging score deltas must filter on this, or the unmeasured rejections enter
        the average as whatever value their absent result defaults to.
        """
        return self.validation is not None

    @property
    def patch_hash(self) -> Optional[str]:
        return self.patch.patch_hash if self.patch else None

    @property
    def validation_before(self) -> Optional[float]:
        return self.validation.score_before if self.validation else None

    @property
    def validation_after(self) -> Optional[float]:
        return self.validation.score_after if self.validation else None

    @property
    def validation_delta(self) -> Optional[float]:
        return self.validation.delta if self.validation else None

    @property
    def graded(self) -> bool:
        """Whether this attempt carries a verdict about the editing strategy.

        An attempt that produced no patch is a statement about the *batch's evidence* --
        the editor had nothing to say -- not about the strategy, so it is excluded from
        layer 3's evidence and from the patch-level statistics.  It stays in the log and
        its count is reported as context.
        """
        return self.outcome in (UPDATE_ACCEPTED, UPDATE_REJECTED,
                                UPDATE_DUPLICATE_PATCH)

    @property
    def archive_pool(self) -> bool:
        """Whether this attempt ends its pool rather than leaving it pending.

        Two ways, and both mean the same thing: *this evidence set has been given its
        full hearing*.

        *   the pool had already reached ``max_pool_batch`` and the patch still failed -- the archive proper;
        *   the editor produced no patch at all from a full pool.  A silent editor is not
            a failed patch, but it is equally an answer, and the only thing the pool can
            offer next time is the same batch.  Re-asking it would spin: every attempt
            would return ``no_proposal``, consume nothing, and leave the pool over the
            trigger -- measured, that loop does not terminate.

        ``pool_cleared`` records the case; this property is the reading of it, so a caller
        asking "does this attempt still own a pool?" has one place to ask.
        """
        return self.pool_cleared

    @property
    def version(self) -> int:
        _, _, tail = self.stable_head_key.rpartition('@v')
        return int(tail) if tail.isdigit() else -1


@dataclass(frozen=True)
class MetaUpdateDecision:

    decision_id: str
    action: str
    head_meta_key: str
    installed_meta_key: Optional[str] = None
    consumed_patch_ids: Tuple[str, ...] = ()
    #: Patch ids the pool held when this decision was made.
    #:
    #: Load-bearing for ``hold``: a hold consumes nothing, so without a recorded pool
    #: size the same hold would be appended again on every subsequent trigger check --
    #: once per completed attempt -- and the decision log would be mostly duplicates of
    #: one unchanged fact.
    pending_at_decision: int = 0
    #: Accept/reject split of the evidence, so a reader can see what the strategy was
    #: revised from without opening every patch record.
    accepted_at_decision: int = 0
    rejected_at_decision: int = 0
    reasons: Tuple[str, ...] = ()
    raw_llm_output: str = ''
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.action not in META_ACTIONS:
            raise ValueError(f'unknown meta action {self.action!r}')
        object.__setattr__(self, 'consumed_patch_ids',
                           tuple(self.consumed_patch_ids))
        object.__setattr__(self, 'reasons', reason_tuple(self.reasons,
                                                        'MetaUpdateDecision'))


def patch_attempt_id(stable_head_key: str, consumed: Sequence[str], trigger: str,
                     outcome: str, attempt_index: int = 0) -> str:
    """Deterministic id for one patch attempt.

    Content-addressed on the head, the evidence, the trigger -- **and the attempt's
    ordinal for that head**.  The ordinal is not decoration: the plan's unit of record is
    *every L2 attempt*, and after a rejection the same head can legitimately
    be asked the same question again on the same evidence -- a resumed run re-deriving an
    attempt, or a test driving two attempts on one pool.  Without the ordinal those two
    attempts would carry one id, and the append-only log would refuse the second record as
    a duplicate of the first, losing the fact that the editor was asked twice.

    The duplicate the design actually forbids is a duplicate *patch*, and that is guarded
    separately by :func:`validation.patch_hash` against the reject buffer -- which is the
    candidate duplicate policy and operates on the proposed text rather than on the attempt.

    Deterministic in its inputs, so a re-derived attempt still names itself identically.
    """
    return content_hash({
        'head': stable_head_key,
        'experiences': list(consumed),
        'trigger': trigger,
        'outcome': outcome,
        'n': int(attempt_index),
    })


def revision_after(skill: 'Skill') -> str:
    return f'{skill.skill_id}@v{skill.version + 1}'


def patch_acceptance_rate(attempts: Sequence[PatchAttempt]) -> Optional[float]:
    """Share of *measured* attempts that were accepted.

    ``None`` when nothing was measured, never 0.0: an unmeasured batch of attempts and
    a batch that was measured and rejected throughout must not compare equal, or a
    strategy would look like it had been tried when it had not.

    The denominator is measured attempts only.  Duplicates were rejected without a
    validation run because their outcome was already known against the same head;
    counting them would let a strategy that repeats itself look badly on a number that
    measures repetition rather than judgement.
    """
    measured = [a for a in attempts if a.measured]
    if not measured:
        return None
    return sum(1 for a in measured if a.accepted) / len(measured)
