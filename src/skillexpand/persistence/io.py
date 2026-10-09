"""Atomic JSON artifacts, write-once freezing, JSONL ledgers and run locks.

This is the lowest persistence layer: it depends only on the standard library
and :mod:`skillexpand.schema`, so every other package may import it.

Frozen identities separate *protocol* from *provenance*.  Every key of a
frozen JSON object must match exactly on resume, except the ``code`` source
fingerprint: a code change is refused by default, and may be accepted with an
explicit ``allow_code_change`` that appends the drift to ``code_changes.jsonl``
beside the frozen file.  The original frozen file is never rewritten.
"""

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from skillexpand import schema as S
from skillexpand.reliability.errors import (
    AuditFailure, FrozenCodeChanged, FrozenProtocolChanged, LedgerCorrupt, RunLocked,
)

#: Frozen-identity keys that record provenance rather than protocol.
PROVENANCE_KEYS = ('code',)
#: Keys that only name the LLM transport.  A relay run binds a fresh loopback port
#: on every launch, so these legitimately differ between a run and its resume.
TRANSPORT_KEYS = ('provider', 'relay_base_url', 'llm_transport', 'direct_provider_fallback')
CODE_CHANGES = 'code_changes.jsonl'


def _relay_run():
    """True for a relay launch (the CLI sets this before any frozen identity is built)."""
    return os.environ.get('EXPE_LLM_RELAY_REQUIRED', '').strip().lower() in (
        '1', 'true', 'yes', 'on')


def _without_transport(obj):
    if isinstance(obj, dict):
        return {k: _without_transport(v) for k, v in obj.items() if k not in TRANSPORT_KEYS}
    if isinstance(obj, list):
        return [_without_transport(v) for v in obj]
    return obj


def require(condition, message):
    if not condition:
        raise AuditFailure(message)


def save(path, data, indent=None):
    """Atomically replace ``path`` with JSON ``data``; a ``None`` path is a no-op."""
    if path is None:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    with temp.open('w') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=indent)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def read_json(path):
    return json.loads(Path(path).read_text())


def code_signature():
    """Content hash of every module in the installed ``skillexpand`` package."""
    source = Path(__file__).resolve().parents[1]
    return {
        str(p.relative_to(source)): S.content_hash(p.read_text(encoding='utf-8'))
        for p in sorted(source.rglob('*.py'))
        if not p.name.startswith('._')
    }


def _code_drift(frozen, current):
    frozen, current = frozen or {}, current or {}
    return {
        'changed': sorted(k for k in set(frozen) & set(current) if frozen[k] != current[k]),
        'added': sorted(set(current) - set(frozen)),
        'removed': sorted(set(frozen) - set(current)),
    }


def _record_code_change(path, frozen, current, drift):
    ledger = path.parent / CODE_CHANGES
    current_hash = S.content_hash(current)
    rows = [r for r in read_jsonl(ledger, repair_tail=False) if r.get('frozen_file') == path.name]
    if rows and rows[-1].get('current_code_hash') == current_hash:
        return
    append_jsonl(ledger, {'frozen_file': path.name, 'time': time.time(),
                          'frozen_code_hash': S.content_hash(frozen),
                          'current_code_hash': current_hash, **drift})


def freeze(path, value, allow_code_change=False):
    """Write ``value`` once; later calls must agree (see the module docstring)."""
    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        save(path, value)
        return
    frozen = json.loads(path.read_text())
    if frozen == value:
        return
    if os.environ.get('SKILLEXPAND_ALLOW_RELAY_CODE_DRIFT') == '1':
        # A relay transport fix must be able to resume an immutable run.  Only
        # runtime code/provider fingerprints may drift; every task, card,
        # Skill, prompt, and protocol input remains frozen.
        def runtime_only(obj):
            if not isinstance(obj, dict):
                return obj
            return {k: runtime_only(v) for k, v in obj.items()
                    if k not in ('code', 'provider', 'relay_base_url',
                                 'llm_transport', 'direct_provider_fallback')}
        if runtime_only(frozen) == runtime_only(value):
            save(path, value)
            return
    if _relay_run():
        # Transport drift only: ignoring it must not hide a source-code change,
        # which still has to pass through ``allow_code_change`` below.
        if _without_transport(frozen) == _without_transport(value):
            return
        frozen_view, value_view = _without_transport(frozen), _without_transport(value)
    else:
        frozen_view, value_view = frozen, value
    if isinstance(frozen_view, dict) and isinstance(value_view, dict):
        strip = lambda d: {k: v for k, v in d.items() if k not in PROVENANCE_KEYS}
        if strip(frozen_view) == strip(value_view) and set(frozen_view) == set(value_view):
            drift = _code_drift(frozen.get('code'), value.get('code'))
            if not allow_code_change:
                files = drift['changed'] + drift['added'] + drift['removed']
                raise FrozenCodeChanged(
                    f'Source code changed since {path} was frozen ({len(files)} files: '
                    f'{", ".join(files[:8])}{" ..." if len(files) > 8 else ""}). '
                    'Use a new run directory, or resume with --allow-code-change to '
                    f'record the drift in {CODE_CHANGES}.')
            _record_code_change(path, frozen.get('code'), value.get('code'), drift)
            return
    raise FrozenProtocolChanged(f'Frozen inputs changed: {path}; use a new run directory')


def read_split(path):
    obj = json.loads(Path(path).read_text())
    return S.SplitPlan.make({int(k): v for k, v in obj['assignment'].items()},
                            obj['benchmark'], obj['seed'], obj.get('families', {}))


def read_jsonl(path, repair_tail=True):
    """Read JSONL records.

    With ``repair_tail`` an interrupted final record is moved to
    ``<file>.interrupted-tail`` and truncated, and a missing final newline is
    restored.  Without it the read is side-effect free and any corrupt record
    raises.  Corruption before the final record always raises.
    """
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
                    raise LedgerCorrupt(f'{path}: corrupt JSONL record {index + 1}') from exc
                path.with_suffix(path.suffix + '.interrupted-tail').write_bytes(line)
                with path.open('r+b') as stream:
                    stream.truncate(offset)
                    stream.flush()
                    os.fsync(stream.fileno())
                return records
        offset += len(line)
    if repair_tail and data and not data.endswith(b'\n'):
        with path.open('ab') as stream:
            stream.write(b'\n')
            stream.flush()
            os.fsync(stream.fileno())
    return records


def append_jsonl(path, record):
    """Append one canonical record and fsync it before returning."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('ab+') as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell():
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b'\n':
                stream.write(b'\n')
        stream.write((S.to_jsonl(record) + '\n').encode('utf-8'))
        stream.flush()
        os.fsync(stream.fileno())


@contextmanager
def exclusive_lock(path):
    """Hold a non-blocking exclusive ``flock`` on ``path`` for the block."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunLocked(f'{path} is held by another process') from exc
        yield stream


class RunLock:
    """An OS-held exclusive lock, released automatically if the process exits."""

    def __init__(self, path) -> None:
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open('a+')
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RunLocked(
                f'{self.path.parent} is already being written by another process') from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f'{os.getpid()}\n')
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
