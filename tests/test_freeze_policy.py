"""Frozen identities contain method inputs only and are immutable."""
import json

import pytest

from skillexpand.persistence import io as IO
from skillexpand.reliability.errors import FrozenProtocolChanged, StoreError
import tests.test_experience_first as EF


def identity():
    return {'protocol': 'p1', 'config': {'k': 4}}


def test_identical_identity_resumes_and_protocol_changes_are_refused(tmp_path):
    path = tmp_path / 'manifest.json'
    IO.freeze(path, identity())
    IO.freeze(path, identity())
    with pytest.raises(FrozenProtocolChanged, match='Frozen inputs changed'):
        IO.freeze(path, dict(identity(), config={'k': 5}))


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


def test_cold_start_resume_refuses_protocol_change():
    fixture = EF.ExperienceFirstTests('test_task_concurrency_and_split_before_clustering')
    fixture.setUp()
    try:
        fixture.cold()
        fixture.cold()
        fixture.cfg.models.cold_start = 'another-model'
        with pytest.raises(FrozenProtocolChanged):
            fixture.cold()
    finally:
        fixture.tearDown()
