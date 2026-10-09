"""Atomic JSON artifacts, write-once freezing, JSONL ledgers and run locks.

This is the lowest persistence layer: it depends only on the standard library
and :mod:`skillexpand.schema`, so every other package may import it.

Frozen identities contain method inputs only; ``freeze`` writes once and any
later call must be equal, else ``FrozenProtocolChanged``.
"""

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path

from skillexpand import schema as S
from skillexpand.reliability.errors import (
    AuditFailure, FrozenProtocolChanged, LedgerCorrupt, RunLocked,
)

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


def freeze(path, value):
    """Write ``value`` once; later calls must be equal."""
    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        save(path, value)
        return
    if json.loads(path.read_text()) != value:
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
