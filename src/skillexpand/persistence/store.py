"""Append-only JSONL store for the Skill version chain.

One record per line, keys sorted.  A committed Skill version is never rewritten;
in-memory state is a pure function of the file, so a crash loses at most the
line being written, and a corrupted final line is detected rather than absorbed.

Layout under the run directory::

    skills.jsonl        every committed Skill version, one per line
"""

import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List

from skillexpand import schema as S
from skillexpand.persistence.io import append_jsonl, read_jsonl
from skillexpand.reliability.errors import StoreError



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
            append_jsonl(self.path, record)

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
