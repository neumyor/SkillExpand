"""Offline-only recovery of consumed slots after the documented disk failure.

Never calls a provider, starts Harbor, reserves a slot, or modifies bound files.
Requires every original driver and any process mentioning the run to be gone.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT/'code')]
from state import TASKS, read, write, verify_freeze
from input_filter import project_tool_results


def optional(path):
    try:
        return read(path)
    except (OSError, ValueError):
        return None


def terminal_evidence():
    evidence = []
    for driver in read(ROOT/'dispatch.json')['drivers']:
        pid = driver['pid']
        stat = Path('/proc')/str(pid)/'stat'
        if stat.exists():
            state = stat.read_text().split(') ', 1)[1].split()[0]
            if state != 'Z':
                raise RuntimeError('Original driver still alive: '+str(pid))
        evidence.append({'pid': pid, 'alive': False, 'evidence': 'proc_missing_or_zombie'})
    for process in Path('/proc').iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            command = (process/'cmdline').read_bytes()
            if str(ROOT).encode() not in command:
                continue
            state = (process/'stat').read_text().split(') ', 1)[1].split()[0]
            if state != 'Z':
                raise RuntimeError('Related process still alive: '+process.name)
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            pass
    return evidence


def preserve(slot, source, entries, replaced=False):
    if not source.exists():
        return
    relative = source.relative_to(slot)
    archive = slot/'infrastructure_originals'/relative.parent/(relative.name+'.raw')
    archive.parent.mkdir(parents=True, exist_ok=True)
    data = source.read_bytes()
    if archive.exists() and archive.read_bytes() != data:
        raise ValueError('Existing infrastructure archive differs')
    archive.write_bytes(data)
    entries.append({'original': str(source.relative_to(ROOT)),
                    'archive': str(archive.relative_to(ROOT)),
                    'bytes': len(data), 'replaced': replaced})


def reconstruct(slot):
    """Give credit only to complete, independently checkable official evidence."""
    from skillexpand import schema as S
    from skillexpand.skill_package import entrypoint_content
    from summarize_tb21_empirical import validate_trial
    from execution import safe_metrics

    record = {'status': 'unresolved', 'reward': None, 'no_retry': True,
              'identity_valid': False, 'error_type': 'InfrastructureWriteFailure',
              'duration_seconds': None, 'tool_observations': []}
    task = slot.relative_to(ROOT).parts[1]
    results = sorted((slot/'jobs').glob('*/*/result.json'))
    if len(results) == 1:
        result_path = results[0]
        record.update(result_path=str(result_path),
                      trajectory_path=str(result_path.parent/'agent/trajectory.json'))
        snapshot = optional(result_path.parent/'snapshot.json')
        if snapshot:
            record.update(sandbox_id=snapshot.get('source_sandbox_id'),
                          snapshot_status=snapshot.get('status'))
    try:
        if len(results) != 1:
            raise ValueError('Missing or multiple official trials')
        frozen = ROOT/'tasks'/task/'frozen'
        executor, protocol = read(frozen/'executor.json'), read(frozen/'protocol.json')
        actual_input = read(slot/'input.json')
        if slot.relative_to(ROOT).parts[2] == 'baseline':
            expected_skill = read(frozen/'seed.json')
        elif slot.parent.name == 'dev':
            expected_skill = read(slot.parent.parent/'skill.json')
        elif slot.parent.name == 'final':
            expected_skill = read(slot.parent/'skill.json')
        else:
            raise ValueError('Unknown executor phase')
        skill = S.from_dict(S.Skill, expected_skill)
        expected_input = {'skill': S.to_dict(skill), 'executor': executor,
                          'protocol': protocol, 'task_path': str(frozen/'tasks'/task)}
        if actual_input != expected_input:
            raise ValueError('Frozen slot input mismatch')
        configs = list((slot/'jobs').glob('*/config.json'))
        if len(configs) != 1:
            raise ValueError('Missing or multiple Harbor jobs')
        job = read(configs[0])
        if (job.get('n_attempts') != 1 or
                job.get('orchestrator', {}).get('retry', {}).get('max_retries') != 0 or
                job.get('orchestrator', {}).get('n_concurrent_trials') != 1):
            raise ValueError('Harbor attempt or retry drift')
        datasets = job.get('datasets') or []
        if len(datasets) != 1 or datasets[0].get('task_names') != [task] or datasets[0].get('path') != str(frozen/'tasks'):
            raise ValueError('Harbor task drift')
        result, reward, trace_path = validate_trial(results[0], task, activation=True,
                                                   model=protocol['model_name'])
        expected_environment = json.loads(json.dumps(executor['environment']))
        expected_environment['kwargs']['skill_root'] = str(slot/'skills')
        config = result['config']
        if (config['agent'] != executor['agent'] or config['verifier'] != executor['verifier'] or
                config['environment'] != expected_environment or
                config['task'] != {**executor['task'], 'path': str(frozen/'tasks'/task)}):
            raise ValueError('Frozen execution configuration mismatch')
        for key in ('timeout_multiplier', 'agent_timeout_multiplier', 'verifier_timeout_multiplier',
                    'agent_setup_timeout_multiplier', 'environment_build_timeout_multiplier'):
            if config.get(key) != executor.get(key):
                raise ValueError('Frozen timeout multiplier mismatch')
        if not record.get('sandbox_id'):
            raise ValueError('Missing authoritative sandbox identity')
        mounted = slot/'skills'/task
        expected_files = {'SKILL.md': entrypoint_content(skill, package=skill.package_mode).encode()}
        if skill.package_mode:
            expected_files.update({name: value.encode() for name, value in skill.resources.items()})
        actual_files = {p.relative_to(mounted).as_posix(): p.read_bytes()
                        for p in mounted.rglob('*') if p.is_file()}
        if actual_files != expected_files or any(p.is_symlink() for p in mounted.rglob('*')):
            raise ValueError('Full mounted package mismatch')
        trace = read(trace_path)
        record.update(status='valid', reward=reward, trajectory_path=str(trace_path),
                      tool_observations=project_tool_results(trace),
                      exception=result.get('exception_info'), metrics=trace.get('final_metrics'))
        safe_metrics(slot, record, skill)
        record['error_type'] = None
        return record, True
    except Exception as exc:
        record.update(status='unresolved', reward=None, identity_valid=False,
                      reconstruction_limit=type(exc).__name__)
        if record.get('trajectory_path') and optional(Path(record['trajectory_path'])) is None:
            record['lost_trajectory_path'] = record.pop('trajectory_path')
        return record, False


def finalize_recovered_paths():
    """Keep unreadable source paths as limitations, not usable feedback paths."""
    verify_freeze()
    processes = terminal_evidence()
    changed = []
    for marker in (ROOT/'tasks').rglob('infra_recovery.json'):
        metadata = read(marker)
        if not metadata.get('record_recovered'):
            continue
        path = marker.parent/'record.json'
        record = read(path)
        trace = record.get('trajectory_path')
        if trace and optional(Path(trace)) is None:
            archive = marker.parent/'infrastructure_originals'/'record-before-lost-path-normalization.json.raw'
            data = path.read_bytes()
            if archive.exists():
                raise ValueError('Path normalization already archived')
            archive.write_bytes(data)
            metadata['preserved_files'].append({'original': str(path.relative_to(ROOT)),
                'archive': str(archive.relative_to(ROOT)), 'bytes': len(data), 'replaced': True})
            record['lost_trajectory_path'] = record.pop('trajectory_path')
            write(marker, metadata)
            write(path, record)
            changed.append(str(path.relative_to(ROOT)))
    damaged_streams = []
    for reservation in (ROOT/'tasks').rglob('reserved.json'):
        if read(reservation)['kind'] != 'executor':
            continue
        slot = reservation.parent
        record = read(slot/'record.json')
        for stream in (slot/'jobs').glob('*/*/agent/raw_model_responses.jsonl'):
            malformed = False
            for line in stream.read_text().splitlines():
                try:
                    json.loads(line)
                except ValueError:
                    malformed = True
            if not malformed:
                continue
            marker = slot/'infra_recovery.json'
            metadata = optional(marker)
            if metadata is None:
                if record.get('status') != 'unresolved' or 'OSError' not in record.get('error', ''):
                    raise ValueError('Malformed provider stream lacks disk failure evidence')
                metadata = {'reason': 'host_disk_write_failure', 'no_retry': True,
                    'preserved_files': [], 'terminal_processes': processes,
                    'checked_at': datetime.now(timezone.utc).isoformat(),
                    'source_artifact_lost': True, 'record_recovered': False}
                preserve(slot, slot/'record.json', metadata['preserved_files'])
            if any(e['original'] == str(stream.relative_to(ROOT)) for e in metadata['preserved_files']):
                continue
            preserve(slot, stream, metadata['preserved_files'])
            write(marker, metadata)
            damaged_streams.append(str(stream.relative_to(ROOT)))
    verify_freeze()
    print(json.dumps({'normalized_lost_feedback_paths': changed,
                      'archived_truncated_provider_streams': damaged_streams}))


def main(apply=False):
    verify_freeze()
    processes = terminal_evidence()
    checked = datetime.now(timezone.utc).isoformat()
    plan = []
    for reservation in sorted((ROOT/'tasks').rglob('reserved.json')):
        if 'infrastructure_originals' in reservation.parts:
            continue
        slot = reservation.parent
        kind = read(reservation)['kind']
        paths = list(slot.glob('*.json')) if kind == 'generation' else list(slot.rglob('*.json'))
        broken = [p for p in paths if 'infrastructure_originals' not in p.parts and optional(p) is None]
        target = slot/('generation_status.json' if kind == 'generation' else 'record.json')
        needs_replacement = optional(target) is None
        no_retry_missing = kind == 'executor' and optional(target) is not None and 'no_retry' not in read(target)
        if not broken and not needs_replacement and not no_retry_missing:
            continue
        if (slot/'infra_recovery.json').exists():
            raise ValueError('Recovery already applied; audit it instead of repeating')
        row = {'slot': str(slot.relative_to(ROOT)), 'kind': kind,
               'broken': [str(p.relative_to(slot)) for p in broken],
               'replace_wrapper': needs_replacement}
        plan.append(row)
        if not apply:
            continue
        entries = []
        for path in broken:
            preserve(slot, path, entries, replaced=path == target and needs_replacement)
        if no_retry_missing and target not in broken:
            preserve(slot, target, entries)
        if kind == 'generation':
            if (slot/'skill.json').exists():
                raise ValueError('Interrupted generation unexpectedly has a package')
            if (slot/'response.sse').exists():
                if b'data: [DONE]' in (slot/'response.sse').read_bytes():
                    raise ValueError('Complete generation stream requires independent reconstruction review')
                preserve(slot, slot/'response.sse', entries)
            if not entries:
                preserve(slot, reservation, entries)
            marker = {'reason': 'host_disk_write_failure', 'no_retry': True,
                      'preserved_files': entries, 'terminal_processes': processes,
                      'checked_at': checked, 'source_artifact_lost': True,
                      'generator_sse_complete': False}
            write(slot/'infra_recovery.json', marker)
            if needs_replacement:
                write(target, {'status': 'interrupted_unresolved', 'no_retry': True,
                               'error_type': 'InfrastructureWriteFailure'})
        else:
            record, official = reconstruct(slot) if needs_replacement else (read(target), False)
            if not entries:
                preserve(slot, reservation, entries)
            marker = {'reason': 'host_disk_write_failure', 'no_retry': True,
                      'preserved_files': entries, 'terminal_processes': processes,
                      'checked_at': checked, 'source_artifact_lost': not official,
                      'record_recovered': needs_replacement,
                      'record_reconstructed_from_official': official}
            write(slot/'infra_recovery.json', marker)
            if needs_replacement:
                write(target, record)
    if apply:
        from execution import register_identity
        preserved_summaries = []
        for p in sorted((ROOT/'tasks').rglob('reserved.json')):
            if 'infrastructure_originals' in p.parts:
                continue
            if read(p)['kind'] != 'executor':
                continue
            slot = p.parent
            record = read(slot/'record.json')
            before = json.loads(json.dumps(record))
            register_identity(slot, record)
            if record != before:
                write(slot/'record.json', record)
            if 'dev' in slot.relative_to(ROOT).parts:
                trace = optional(Path(record.get('trajectory_path', '/nonexistent'))) or {'steps': []}
                write(slot/'feedback.json', {'status': record['status'],
                      'reward': record.get('reward') if record['status'] == 'valid' else None,
                      'tool_observations': project_tool_results(trace),
                      'projection': 'fixed-tool-results-8192-deny-evaluation-v1'})
        for task in TASKS:
            baseline = ROOT/'tasks'/task/'baseline'
            records = [read(baseline/f'{i:02d}'/'record.json') for i in range(1, 6)]
            summary = {
                'rewards': [r.get('reward') if r['status'] == 'valid' else None for r in records],
                'valid': sum(r['status'] == 'valid' for r in records),
                'successes': sum(r['status'] == 'valid' and r.get('reward') == 1 for r in records)}
            summary_path = baseline/'summary.json'
            if summary_path.exists():
                archive = ROOT/'infrastructure_summaries'/(task+'-baseline-summary.json.raw')
                archive.parent.mkdir(parents=True, exist_ok=True)
                original = summary_path.read_bytes()
                if archive.exists() and archive.read_bytes() != original:
                    raise ValueError('Existing summary archive differs')
                archive.write_bytes(original)
                preserved_summaries.append({'original': str(summary_path.relative_to(ROOT)),
                                           'archive': str(archive.relative_to(ROOT)), 'bytes': len(original)})
            write(summary_path, summary)
        write(ROOT/'infrastructure_recovery_summary.json', {
            'checked_at': checked, 'reason': 'host_disk_write_failure',
            'no_provider_or_executor_calls': True, 'no_retry': True, 'slots': plan,
            'preserved_summaries': preserved_summaries})
        verify_freeze()
    print(json.dumps({'apply': apply, 'slots': plan}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--finalize-paths', action='store_true')
    args = parser.parse_args()
    if args.finalize_paths:
        finalize_recovered_paths()
    else:
        main(args.apply)
