"""Frozen identities: protocol is immutable, source drift is explicit and logged."""
import json
from unittest.mock import patch

import pytest

from skillexpand.persistence import io as IO
from skillexpand.reliability.errors import FrozenCodeChanged, StoreError
import tests.test_experience_first as EF


def identity(code):
    return {'protocol': 'p1', 'config': {'k': 4}, 'code': code}


def test_identical_identity_resumes_and_protocol_changes_are_refused(tmp_path):
    path = tmp_path / 'manifest.json'
    IO.freeze(path, identity({'a.py': '1'}))
    IO.freeze(path, identity({'a.py': '1'}))
    with pytest.raises(ValueError, match='Frozen inputs changed'):
        IO.freeze(path, dict(identity({'a.py': '1'}), config={'k': 5}))
    with pytest.raises(ValueError, match='Frozen inputs changed'):
        IO.freeze(path, dict(identity({'a.py': '2'}), config={'k': 5}), allow_code_change=True)
    assert not (tmp_path / IO.CODE_CHANGES).exists()


def test_code_drift_requires_explicit_permission_and_is_logged_once(tmp_path):
    path = tmp_path / 'manifest.json'
    IO.freeze(path, identity({'a.py': '1', 'b.py': '1'}))
    original = path.read_bytes()
    drifted = identity({'a.py': '2', 'c.py': '1'})
    with pytest.raises(FrozenCodeChanged, match='--allow-code-change'):
        IO.freeze(path, drifted)
    IO.freeze(path, drifted, allow_code_change=True)
    IO.freeze(path, drifted, allow_code_change=True)
    rows = IO.read_jsonl(tmp_path / IO.CODE_CHANGES, repair_tail=False)
    assert len(rows) == 1
    assert rows[0]['frozen_file'] == 'manifest.json'
    assert (rows[0]['changed'], rows[0]['added'], rows[0]['removed']) == (['a.py'], ['c.py'], ['b.py'])
    assert path.read_bytes() == original


def test_returning_to_an_earlier_code_version_is_logged(tmp_path):
    path = tmp_path / 'manifest.json'
    IO.freeze(path, identity({'a.py': 'A'}))
    for version in ('B', 'C', 'B'):
        IO.freeze(path, identity({'a.py': version}), allow_code_change=True)
    rows = IO.read_jsonl(tmp_path / IO.CODE_CHANGES, repair_tail=False)
    assert len(rows) == 3


def test_append_after_a_record_without_final_newline_keeps_records_separate(tmp_path):
    path = tmp_path / 'ledger.jsonl'
    path.write_text(json.dumps({'id': 1}))
    IO.append_jsonl(path, {'id': 2})
    assert IO.read_jsonl(path, repair_tail=False) == [{'id': 1}, {'id': 2}]


def test_side_effect_free_jsonl_read_never_repairs(tmp_path):
    path = tmp_path / 'ledger.jsonl'
    path.write_text(json.dumps({'id': 1}))
    assert IO.read_jsonl(path, repair_tail=False) == [{'id': 1}]
    assert path.read_text() == json.dumps({'id': 1})
    path.write_text(json.dumps({'id': 1}) + '\n{"id"')
    with pytest.raises(StoreError):
        IO.read_jsonl(path, repair_tail=False)
    assert IO.read_jsonl(path) == [{'id': 1}]
    assert path.read_text() == json.dumps({'id': 1}) + '\n'


def test_cold_start_resume_refuses_code_drift_unless_allowed():
    from skillexpand.l1 import cold_start as C

    fixture = EF.ExperienceFirstTests('test_task_concurrency_and_split_before_clustering')
    fixture.setUp()
    try:
        fixture.cold()
        with patch.object(C, 'code_signature', return_value={'changed.py': 'x'}):
            with pytest.raises(FrozenCodeChanged):
                fixture.cold()
            C.ColdStart(fixture.cfg, fixture.plan, fixture.root, k=1, cold_start_workers=2,
                        family_discovery_workers=2, ask=fixture.ask,
                        run_units=fixture.units, card_batch_size=1, allow_code_change=True)
        rows = IO.read_jsonl(fixture.root / IO.CODE_CHANGES, repair_tail=False)
        assert [r['frozen_file'] for r in rows] == ['manifest.json']
    finally:
        fixture.tearDown()
