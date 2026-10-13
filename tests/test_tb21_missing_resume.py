import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import resume_tb21_e4_e7_missing as recovery


def test_accepted_slot_returns_without_reservation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / 'runs' / recovery.TARGETS['E7'][0]
    recovery.write(root / 'slots/67-2/record.json', {'status': 'valid'})
    with patch.object(recovery.aligned, 'reserve') as reserve:
        recovery.worker('E7')
    reserve.assert_not_called()


def test_changed_request_history_blocks_launch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / 'runs' / recovery.TARGETS['E4'][0]
    recovery.write(root / 'slots/09-2/record.json', {'status': 'infrastructure_error'})
    (root / 'slots/09-2/requests').mkdir()
    with patch.object(recovery.aligned, 'reserve') as reserve:
        with pytest.raises(ValueError, match='history changed'):
            recovery.worker('E4')
    reserve.assert_not_called()
