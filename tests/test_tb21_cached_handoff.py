import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

import resume_tb21_policy_tail as tail


def test_deferred_launch_waits_for_original_process_then_skips_valid_slots(tmp_path):
    control = tmp_path / 'runs' / 'wait'
    control.mkdir(parents=True)
    tail.write(control / 'manifest.json', {
        'pipeline_cache': {'version': 'v6'},
        'predecessors': [{'processes': [{'pid': 123, 'start_ticks': 'original'}]}],
    })
    events = []
    with (tmp_path / 'lock').open('a') as lock:
        with patch.object(tail, 'process_identity', side_effect=['original', 'reused']), \
                patch.object(tail.time, 'sleep', lambda seconds: events.append('wait')), \
                patch.object(tail, 'cache_settings', return_value={'version': 'v6'}), \
                patch.object(tail, 'launch_ready', side_effect=lambda *args: events.append('launch')):
            tail.deferred_launch(control, os.dup(lock.fileno()))
    assert events == ['wait', 'launch']
    assert tail.read(control / 'status.json')['phase'] == 'no_unresolved_target_slots'


def test_queued_handoff_reserves_zero_workers_and_passes_cache_environment(tmp_path):
    (tmp_path / 'runs').mkdir()
    old = tmp_path / 'old'
    old.mkdir()
    tail.write(old / 'status.json', {'pid': os.getpid()})
    tail.write(old / 'children.json', {})
    env = {'TB21_PIPELINE_CACHE': '1', 'TB21_PIPELINE_CACHE_VERSION': 'v6'}
    with patch.object(tail.subprocess, 'Popen', return_value=Mock(pid=123)) as spawn:
        tail.queue_cached_launch(tmp_path, env, [old], {'version': 'v6'})
    control = next((tmp_path / 'runs').glob('tb21-pipeline-cache-wait-*'))
    manifest = json.loads((control / 'manifest.json').read_text())
    assert manifest['workers'] == 0
    assert manifest['budget_resets'] == 0
    assert manifest['max_requests_per_slot_this_launch'] == 3
    assert manifest['predecessors'][0]['processes'][0]['start_ticks'] == tail.process_identity(os.getpid())
    assert spawn.call_args.kwargs['env'] == env
    assert spawn.call_args.kwargs['pass_fds']


def test_process_identity_handles_missing_and_zombie_processes():
    with patch.object(Path, 'read_text', side_effect=FileNotFoundError):
        assert tail.process_identity(123) is None
    with patch.object(Path, 'read_text', return_value='123 (process name) Z ' + '0 ' * 19):
        assert tail.process_identity(123) is None
