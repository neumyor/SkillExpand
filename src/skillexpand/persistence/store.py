"""Append-only persistence for the three knowledge layers.

Storage format is JSONL, one record per line, keys sorted.  Three reasons:

*   **Append-only.**  A committed skill version and a recorded patch attempt are never
    rewritten.  The meta layer's evidence cannot be edited after the fact, which is the
    whole point of storing rejected patches too.
*   **Replayable.**  In-memory state is a pure function of the file, so a crash
    mid-run loses at most the line being written, and a corrupted trailing line
    is detectable rather than silently absorbed.
*   **Diffable.**  A skill edit between two versions shows up as a readable text
    diff, which is what makes the paper's qualitative examples cheap to produce.

Layout under the run directory::

    skills.jsonl           every committed skill version, one per line
    experiences.jsonl      layer-1 task experience
    patch_attempts.jsonl   every layer-2 patch attempt, accepted or not
    panel_scores.jsonl     cached held-out panel measurements
    meta_skills.jsonl      every editing-strategy version
    meta_decisions.jsonl   every layer-3 decision

Nothing here imports ExpeL or an LLM, so the persistence contract is testable on
its own.
"""

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from skillexpand import schema as S

DEFAULT_META_SKILL_BODY = """\
When updating a skill:

- Distinguish task-specific failures from structural ones. An individual entity, answer,
  or task instance is task-specific and must never enter the skill.
- Prefer the minimal edit that explains the observed failure. Do not restate
  procedures that already worked.
- Only generalise a mechanism that the experience card actually supports.
- Keep the routing description unchanged; edit only execution rules.
- Cold-start cards ran without a Skill; their outcomes do not measure the current body.
- Preserve behaviour that previously succeeded; an edit that trades one
  capability for another must say so explicitly.
- State each rule so it can be checked against a single trajectory. Avoid
  compound rules that hide which clause did the work.
"""


class StoreError(RuntimeError):
    """Raised when persisted state is internally inconsistent."""


def read_jsonl(path, repair_tail=True):
    """Recover only an interrupted final record; reject corruption in the middle."""
    path = Path(path)
    if not path.exists():
        return []
    data = path.read_bytes()
    lines = data.splitlines(keepends=True)
    records = []
    offset = 0
    for index, line in enumerate(lines):
        if line.strip():
            try:
                records.append(json.loads(line))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                if index != len(lines) - 1 or not repair_tail:
                    raise StoreError(f'{path}: corrupt JSONL record {index + 1}') from exc
                path.with_suffix(path.suffix + '.interrupted-tail').write_bytes(line)
                with path.open('r+b') as stream:
                    stream.truncate(offset)
                    stream.flush()
                    os.fsync(stream.fileno())
                return records
        offset += len(line)
    if data and not data.endswith(b'\n'):
        with path.open('ab') as stream:
            stream.write(b'\n')
            stream.flush()
            os.fsync(stream.fileno())
    return records


class _JsonlStore:

    #: Shared across every instance: two stores for the same file in one process is a
    #: configuration mistake, but a shared lock makes it harmless rather than corrupt.
    _file_locks: Dict[str, threading.RLock] = {}
    _registry_lock = threading.Lock()

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _JsonlStore._registry_lock:
            self._lock = _JsonlStore._file_locks.setdefault(
                str(self.path.resolve()), threading.RLock())

    def append(self, record: Any) -> None:
        # fsync per record: an interrupted evolution run must not lose the
        # episode it just finished, because re-running costs real LLM time.
        with self._lock:
            with self.path.open('a') as fh:
                fh.write(S.to_jsonl(record) + '\n')
                fh.flush()
                os.fsync(fh.fileno())

    def read_lines(self) -> Iterator[str]:
        if not self.path.exists():
            return
        with self.path.open() as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                yield line

    def records(self, cls: Any, tolerate_truncated_tail: bool = True) -> List[Any]:
        with self._lock:
            return [S.from_dict(cls, record) for record in
                    read_jsonl(self.path, repair_tail=tolerate_truncated_tail)]


class SkillLibrary:

    def __init__(self, path: Path, benchmark: str = 'alfworld') -> None:
        self.benchmark = benchmark
        self._store = _JsonlStore(Path(path))
        self._lock = threading.RLock()
        self._versions: Dict[str, List[S.Skill]] = {}
        self._replay()

    # -- construction ------------------------------------------------------
    @staticmethod
    def skill_id_for(benchmark: str, family_id: str) -> str:
        return f'{benchmark}.{family_id}'

    # -- reads -------------------------------------------------------------
    @property
    def families(self) -> List[str]:
        return sorted({v[0].family_id for v in self._versions.values()})

    def history(self, family_id: str) -> List[S.Skill]:
        key = self.skill_id_for(self.benchmark, family_id)
        return list(self._versions.get(key, ()))

    def head(self, family_id: str) -> S.Skill:
        history = self.history(family_id)
        if not history:
            raise KeyError(f'family {family_id!r} is not in the library')
        return history[-1]

    def all_heads(self) -> Dict[str, S.Skill]:
        return {f: self.head(f) for f in self.families}

    def get(self, skill_key: str) -> S.Skill:
        skill_id, _, version = skill_key.rpartition('@v')
        if not version.isdigit():
            raise KeyError(f'malformed skill key {skill_key!r}')
        for skill in self._versions.get(skill_id, ()):
            if skill.version == int(version):
                return skill
        raise KeyError(f'unknown skill revision {skill_key!r}')

    def is_empty(self, family_id: str) -> bool:
        return not self.head(family_id).body.strip()

    # -- writes ------------------------------------------------------------
    def commit(self, candidate: S.CandidateSkill) -> S.Skill:
        """Promote a candidate. Only layer 2's accept path may call this.

        The committed revision is the candidate's skill with its provenance
        linked to the episode, so ``skill -> episode -> evaluation`` is traceable
        in both directions.

        The whole check-then-append is serialised.  Layer-2 updates run on worker
        threads, and the version-chain check is a read followed by a write: two threads
        committing to the same skill could both see the same head and both append
        ``head + 1``, producing exactly the gap this class exists to prevent.
        """
        with self._lock:
            return self._commit_locked(candidate)

    def _commit_locked(self, candidate: S.CandidateSkill) -> S.Skill:
        # Raised as a StoreError rather than letting `head()` leak a KeyError: a
        # candidate that names a skill outside the library is the one failure mode
        # that would silently grow the skill set, and "could not commit" is a more
        # accurate description than "no such key".
        if candidate.skill.skill_id not in self._versions:
            raise StoreError(
                f'candidate targets {candidate.skill.skill_id!r}, which is not one '
                f'of the {len(self._versions)} skills in this library; the skill set '
                'is fixed after initialisation and may never be extended')
        head = self.head(candidate.skill.family_id)
        if candidate.skill.version != head.version + 1:
            raise StoreError(
                f'candidate {candidate.skill.key} does not follow head '
                f'{head.key}; refusing to create a gap in the version chain')
        if candidate.base_skill_key != head.key:
            raise StoreError(
                f'candidate was proposed against {candidate.base_skill_key} but '
                f'head is {head.key}; a stale proposal cannot be committed')
        self._append_new(candidate.skill)
        return candidate.skill

    def _append_new(self, skill: S.Skill) -> None:
        existing = self._versions.setdefault(skill.skill_id, [])
        if existing and skill.version != existing[-1].version + 1:
            raise StoreError(
                f'{skill.key} breaks the version chain '
                f'(head is {existing[-1].key})')
        self._store.append(skill)
        existing.append(skill)

    def _replay(self) -> None:
        for skill in self._store.records(S.Skill):
            # ``history(family)`` looks the chain up by ``skill_id_for(benchmark,
            # family)``, so a library loaded with a different benchmark than the one it
            # was written with silently returns an empty history -- the skills are on
            # disk and the reader reports none.  Loud beats silent here.
            if not skill.skill_id.startswith(f'{self.benchmark}.'):
                raise StoreError(
                    f'{self._store.path}: record {skill.key} belongs to a different '
                    f'benchmark than the {self.benchmark!r} this library was opened '
                    f'with; every lookup would silently miss. Reopen it with '
                    f"benchmark={skill.skill_id.split('.')[0]!r}.")
            existing = self._versions.setdefault(skill.skill_id, [])
            if existing and skill.version != existing[-1].version + 1:
                raise StoreError(
                    f'{self._store.path}: {skill.key} breaks the version chain '
                    f'after {existing[-1].key}')
            existing.append(skill)

    def render(self) -> str:
        """Human-readable summary, for logs and for the paper's appendix."""
        lines = []
        for family in self.families:
            for skill in self.history(family):
                body = skill.body.strip() or '(empty)'
                lines.append(f'{skill.key}\n    {body}\n')
        return '\n'.join(lines)


class MetaSkillStore:
    """Version chain of meta-skills: the policy that conditions the Attributor.

    Meta-skill v0 is the built-in default policy.  Every later version records
    which episodes it was synthesised from, so a version's derivation is auditable
    and, critically, so that prospective evaluation can restrict itself to
    episodes the version never saw.
    """

    def __init__(self, path: Path) -> None:
        self._store = _JsonlStore(Path(path))
        self._versions: List[S.MetaSkill] = []
        self._replay()

    def _replay(self) -> None:
        for meta in self._store.records(S.MetaSkill):
            self._versions.append(meta)

    def ensure_initial(self) -> S.MetaSkill:
        if not self._versions:
            meta = S.MetaSkill(
                version=0, body=DEFAULT_META_SKILL_BODY, parent_version=None,
                rationale='built-in default consolidation policy')
            self._store.append(meta)
            self._versions.append(meta)
        return self.head()

    def head(self) -> S.MetaSkill:
        if not self._versions:
            raise StoreError('meta-skill store is empty; call ensure_initial()')
        return self._versions[-1]

    def get(self, version: int) -> S.MetaSkill:
        for meta in self._versions:
            if meta.version == version:
                return meta
        raise KeyError(f'unknown meta-skill version {version}')

    def history(self) -> List[S.MetaSkill]:
        return list(self._versions)

    def append(self, meta: S.MetaSkill) -> None:
        if self._versions and meta.version != self._versions[-1].version + 1:
            raise StoreError(
                f'{meta.key} breaks the meta version chain '
                f'(head is {self._versions[-1].key})')
        self._store.append(meta)
        self._versions.append(meta)


class PatchAttemptLog:
    """Every layer-2 patch attempt: the skill pool's consumptive record.

    This log carries two distinct pieces of state at once, deliberately.

    1.  It is the **output** of layer 2 -- each record is one patch attempt, accepted or
        rejected, which is what the plan sends to the Meta Pool.
    2.  It is the **bookkeeping** that makes the skill pools replayable.  An experience
        is pending exactly when no *accepted* attempt lists it in
        ``pooled_experience_ids``, so the pending set is derived from the log rather than
        stored beside it, and the pools cannot disagree with the attempts that drained
        them.

    The consumption rule is the pool policy.  A rejected patch consumes nothing --
    its pool stays intact because the problem it describes has not been solved -- so only
    accepted attempts subtract.  The rest of the lifecycle follows from that one line:
    a rejection leaves the pool alone, an acceptance empties it (every pending experience
    was collected under the head that just changed), and a rejection at the pool's
    capacity is handled by the registry archiving rather than by a second consumption
    path here.
    """

    def __init__(self, path: Path) -> None:
        self._store = _JsonlStore(Path(path))
        self._patches: List[S.PatchAttempt] = []
        self._by_id: Dict[str, S.PatchAttempt] = {}
        self._consumed_patch_ids: set = set()
        self._replay()

    def append(self, attempt: S.PatchAttempt) -> None:
        if attempt.patch_id in self._by_id:
            raise StoreError(
                f'patch attempt {attempt.patch_id} already recorded; '
                'patch attempts are immutable')
        self._store.append(attempt)
        self._patches.append(attempt)
        self._by_id[attempt.patch_id] = attempt

    def __len__(self) -> int:
        return len(self._patches)

    def __iter__(self) -> Iterator[S.PatchAttempt]:
        return iter(self._patches)

    def all(self) -> List[S.PatchAttempt]:
        return list(self._patches)

    def get(self, patch_id: str) -> S.PatchAttempt:
        return self._by_id[patch_id]

    def for_skill(self, skill_id: str) -> List[S.PatchAttempt]:
        return [a for a in self._patches if a.skill_id == skill_id]

    def for_meta_version(self, version: int) -> List[S.PatchAttempt]:
        return [a for a in self._patches if a.meta_skill_version == version]

    def accepted(self) -> List[S.PatchAttempt]:
        return [a for a in self._patches if a.accepted]

    def rejected(self) -> List[S.PatchAttempt]:
        return [a for a in self._patches if a.rejected]

    def consumed_experience_ids(self) -> set:
        """Completed fixed-batch evidence; also supports historical online logs.

        Serial L2 finishes a batch on every verdict, including no proposal. Historical
        online runs only consumed evidence on acceptance or pool archival.
        """
        out: set = set()
        for attempt in self._patches:
            if attempt.trigger == S.TRIGGER_FIXED_BATCH or attempt.accepted or attempt.pool_cleared:
                out.update(attempt.pooled_experience_ids)
        return out

    def consumed_patch_ids(self) -> set:
        """Patch ids layer 3 has already learned from.

        Read from the *decision* log rather than inferred from the patch log: consumption
        is a layer-3 act, and an attempt is pending for layer 3 until a synthesis decision
        names it.
        """
        return self._consumed_patch_ids

    def mark_consumed(self, patch_ids: Sequence[str]) -> None:
        """Record that layer 3 has consumed these attempts.

        Kept in memory rather than in a file of its own, because it is derived state with
        an authoritative record elsewhere: the decision log holds every synthesis, and
        ``MetaPool`` read ``consumed_patch_ids`` from here while the
        process runs.  On resume the loop replays the decision log into this set, so the
        two cannot disagree after the fact any more than they can disagree live.
        """
        self._consumed_patch_ids.update(str(p) for p in patch_ids)

    @property
    def acceptance_rate(self) -> Optional[float]:
        return S.patch_acceptance_rate(self._patches)

    def _replay(self) -> None:
        for attempt in self._store.records(S.PatchAttempt):
            self._patches.append(attempt)
            self._by_id[attempt.patch_id] = attempt


class MetaDecisionLog:
    """Every layer-3 event: a strategy synthesis, or a recorded hold.

    Holds are recorded rather than skipped.  "A synthesis was due and could not be
    made" is a fact about the experiment's power -- the patch evidence was too thin, or
    the model produced nothing usable -- and silently doing nothing would make an
    under-evidenced meta layer look like one that simply chose not to revise.
    """

    def __init__(self, path: Path) -> None:
        self._store = _JsonlStore(Path(path))
        self._decisions: List[S.MetaUpdateDecision] = []
        self._replay()

    def append(self, decision: S.MetaUpdateDecision) -> None:
        self._store.append(decision)
        self._decisions.append(decision)

    def __len__(self) -> int:
        return len(self._decisions)

    def all(self) -> List[S.MetaUpdateDecision]:
        return list(self._decisions)

    def by_action(self, action: str) -> List[S.MetaUpdateDecision]:
        return [d for d in self._decisions if d.action == action]

    def consumed_patch_ids(self) -> set:
        out = set()
        for decision in self._decisions:
            out.update(decision.consumed_patch_ids)
        return out

    def last_hold_reason(self) -> Optional[str]:
        """The reason on the most recent hold, for de-duplicating a repeated hold."""
        for decision in reversed(self._decisions):
            if decision.action == S.META_ACTION_HOLD:
                return decision.reasons[0] if decision.reasons else ''
        return None

    def _replay(self) -> None:
        for decision in self._store.records(S.MetaUpdateDecision):
            self._decisions.append(decision)


class SelectionAttemptLog:

    def __init__(self, path: Path) -> None:
        self._store = _JsonlStore(Path(path))
        self._attempts: List[S.SelectionAttempt] = []
        for rec in self._store.records(S.SelectionAttempt):
            self._attempts.append(rec)

    def append(self, attempt: S.SelectionAttempt) -> None:
        self._store.append(attempt)
        self._attempts.append(attempt)

    def __len__(self) -> int:
        return len(self._attempts)

    def all(self) -> List[S.SelectionAttempt]:
        return list(self._attempts)

    def failed(self) -> List[S.SelectionAttempt]:
        return [a for a in self._attempts if not a.succeeded]

    @property
    def success_rate(self) -> Optional[float]:
        """None when nothing was attempted -- never 0.0."""
        if not self._attempts:
            return None
        return sum(1 for a in self._attempts if a.succeeded) / len(self._attempts)
