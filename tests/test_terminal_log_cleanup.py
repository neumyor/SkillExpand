import importlib.util
import json
from pathlib import Path


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / 'scripts' / f'{name}.py')
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_cleanup_only_completed_regular_closed_files(tmp_path):
    cleanup = module('cleanup_terminal_logs')
    agent = tmp_path / 'trial/agent'
    agent.mkdir(parents=True)
    log = agent / 'terminal.pane'
    log.write_text('output')
    result = agent.parent / 'result.json'
    assert cleanup.eligibility(log, tmp_path, set())[1] == 'missing_result'
    result.write_text('{')
    assert cleanup.eligibility(log, tmp_path, set())[1] == 'invalid_result'
    result.write_text(json.dumps({'finished_at': None}))
    assert cleanup.eligibility(log, tmp_path, set())[1] == 'unfinished'
    result.write_text(json.dumps({'finished_at': '2026-10-14T00:00:00Z'}))
    assert cleanup.eligibility(log, tmp_path, set())[0]['size'] == 6
    st = log.stat()
    assert cleanup.eligibility(log, tmp_path, {(st.st_dev, st.st_ino)})[1] == 'open'
    link = agent / 'link.cast'
    link.symlink_to(log)
    assert cleanup.eligibility(link, tmp_path, set())[1] == 'not_regular'
