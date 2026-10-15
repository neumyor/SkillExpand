"""Single-shot, budgeted old/evolved x single/autonomous loading experiment."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / 'code'), str(ROOT)]
from skillexpand import schema as S
from skillexpand.evaluation.selector import parse_selection, parse_why
from skillexpand.runtime.progressive import catalog, select_and_load, autonomous_catalog_instruction
from skillexpand.runtime.llm_relay import relay_from_env, coalesce_sse
from skillexpand.skill_package import materialize_skill
from summarize_tb21_empirical import validate_trial
from runtime import build_config
from study import CELLS, slot_path, collect_loading_evidence


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f'.{os.getpid()}.{threading.get_ident()}.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def safe_error(exc):
    value = str(exc)
    for name in ('OPENAI_API_KEY', 'TB21_EXECUTOR_API_KEY', 'E2B_API_KEY'):
        secret = os.environ.get(name)
        if secret:
            value = value.replace(secret, '[REDACTED]')
    return value[-3000:]


def verify_freeze():
    for item in read(ROOT / 'bindings.json'):
        if (ROOT / item['current']).read_bytes() != (ROOT / item['snapshot']).read_bytes():
            raise ValueError('Frozen source changed: ' + item['current'])
    if (ROOT / 'integrity_abort.json').exists():
        raise ValueError('Identity integrity failure blocks new calls')


def reserve(row):
    verify_freeze()
    manifest = read(ROOT / 'manifest.json')
    if row not in read(ROOT / 'frozen/schedule.json'):
        raise ValueError('Unallocated slot')
    slot = slot_path(ROOT, manifest['stage'], row)
    with (ROOT / '.slots.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (slot / 'reserved.json').exists():
            return slot, False
        count = len(list((ROOT / manifest['stage']).glob('slots/*/*/*/reserved.json')))
        if count >= manifest['executor_planned'] or shutil.disk_usage(ROOT).free < 20 * 1024**3:
            raise RuntimeError('Budget or storage admission blocked')
        write(slot / 'reserved.json', {'kind': 'executor', 'slot': str(slot.relative_to(ROOT)),
                                       'at': time.time(), 'no_retry': True, **row})
    return slot, True


class SelectorHost:
    benchmark_name = 'terminalbench'

    def __init__(self, transport, slot):
        self.transport, self.slot = transport, slot

    def llm(self, messages, **kwargs):
        payload = {**read(ROOT / 'frozen/selector_settings.json'), 'messages': [
            {'role': {'human': 'user', 'ai': 'assistant'}.get(m.type, m.type), 'content': m.content}
            for m in messages]}
        write(self.slot / 'selector_request.json', payload)
        try:
            with (self.slot / 'selector_response.sse').open('wb') as stream:
                def chunks():
                    for chunk in self.transport.stream(payload):
                        chunk = chunk.encode() if isinstance(chunk, str) else chunk
                        stream.write(chunk)
                        stream.flush()
                        yield chunk
                response = coalesce_sse(chunks())
            raw = response['choices'][0]['message']['content']
            write(self.slot / 'selector_response.json', {**response, 'raw': raw})
            return raw
        except Exception as exc:
            write(self.slot / 'selector_response.json', {'status': 'failed', 'raw': '',
                                                       'error': safe_error(exc)})
            raise


def claim_identity(slot, identity):
    with (ROOT / '.identities.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ledger = read(ROOT / 'identity_ledger.json') if (ROOT / 'identity_ledger.json').exists() else {}
        if not identity:
            raise ValueError('Missing authoritative sandbox identity')
        if identity in ledger or identity in read(ROOT / 'frozen/historical_sandbox_ids.json'):
            write(ROOT / 'integrity_abort.json', {'sandbox_id': identity, 'slot': str(slot)})
            raise ValueError('Duplicate or historical sandbox identity')
        ledger[identity] = str(slot.relative_to(ROOT))
        write(ROOT / 'identity_ledger.json', ledger)


def materialize(slot, skills, mode, task, transport):
    visible = catalog(skills)
    write(slot / 'catalog.json', visible)
    if mode == 'single':
        selected, selection = select_and_load(SelectorHost(transport, slot), task['instruction'], skills)
        write(slot / 'selection.json', selection)
        mounted = [selected]
        suffix = ('\n\nPlease read the skill first. Read /opt/openclaw-skills/'
                  + selected.skill_id + '/SKILL.md before executing task commands.')
        write(slot / 'loaded_skill.json', S.to_dict(selected))
    else:
        mounted = skills
        suffix = autonomous_catalog_instruction(skills)
    files = {}
    for skill in mounted:
        directory = slot / 'skills' / skill.skill_id
        materialize_skill(skill, directory, package=skill.package_mode)
        for path in sorted(directory.rglob('*')):
            if path.is_file():
                relative = path.relative_to(directory).as_posix()
                remote = '/opt/openclaw-skills/' + skill.skill_id + '/' + relative
                files[remote] = {'skill_id': skill.skill_id, 'relative_path': relative,
                                 'content': path.read_text()}
    manifest = {'mode': mode, 'catalog': visible, 'skill_ids': [s.skill_id for s in mounted],
                'files': files, 'initial_suffix': suffix,
                'load_stage': 'after_selection' if mode == 'single' else 'executor_autonomous'}
    write(slot / 'load_manifest.json', manifest)
    return manifest


def execute(row, transport):
    slot, admitted = reserve(row)
    if not admitted:
        if (slot / 'record.json').exists():
            return read(slot / 'record.json')
        raise RuntimeError('Consumed interrupted slot cannot be replaced')
    started = time.time()
    library_name, mode = CELLS[row['cell']]
    task = next(t for t in read(ROOT / 'frozen/task_table.json') if t['task_name'] == row['task'])
    library = read(ROOT / f'frozen/libraries/{library_name}.json')
    record = {**row, 'phase': read(ROOT / 'manifest.json')['stage'], 'library': library_name,
              'mode': mode, 'status': 'unresolved', 'reward': None, 'no_retry': True}
    write(slot / 'input.json', {'task': task, 'library': library, **row, 'mode': mode})
    try:
        skills = [S.from_dict(S.Skill, value) for value in library]
        mounted = materialize(slot, skills, mode, task, transport)
        if mode == 'single':
            selection = read(slot / 'selection.json')
            record.update(selected_skill_id=selection['skill_id'], loaded_skill_key=selection['loaded_skill_key'])
        config = build_config(read(ROOT / 'frozen/executor_template.json'), ROOT, slot,
                              row['task'], slot / 'skills', mode)
        write(slot / 'harbor_config.json', config)
        env = dict(os.environ)
        env.update(OPENAI_API_KEY=env['TB21_EXECUTOR_API_KEY'],
                   PYTHONPATH=':'.join([str(ROOT), str(ROOT / 'frozen/runtime/tencent/python'),
                                       str(ROOT / 'frozen/runtime/framework')]))
        with (slot / 'harbor.log').open('a') as stream:
            process = subprocess.run([read(ROOT / 'manifest.json')['harbor_python'],
                str(ROOT / 'runtime.py'), '--config', str(slot / 'harbor_config.json'),
                '--retry-archive-root', str(slot / 'retry_archives'), '--no-export-traces'],
                env=env, stdout=stream, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        record['harbor_returncode'] = process.returncode
        results = list((slot / 'jobs').glob('*/*/result.json'))
        if len(results) != 1:
            raise ValueError('Missing or duplicate official result')
        result_path = results[0]
        record.update(result_path=str(result_path), trajectory_path=str(result_path.parent / 'agent/trajectory.json'))
        snapshot = read(result_path.parent / 'snapshot.json')
        record['sandbox_id'] = snapshot.get('source_sandbox_id')
        claim_identity(slot, record['sandbox_id'])
        result, reward, trace = validate_trial(result_path, row['task'])
        actual, expected = result['config']['agent'], config['agents'][0]
        if {**actual, 'name': expected.get('name')} != expected:
            raise ValueError('Effective official config drift')
        effective = read(result_path.parent / 'agent/effective_agent_config.json')
        if (effective['max_input_tokens'], effective['max_output_tokens'], effective['enable_summarize'],
            effective['proactive_summarization_threshold']) != (262144, 65536, True, 8000):
            raise ValueError('Effective context or summarization drift')
        record.update(status='valid', reward=reward, exception=result.get('exception_info'))
    except Exception as exc:
        record.update(error_type=type(exc).__name__, error=safe_error(exc))
        if mode == 'single' and not (slot / 'selection.json').exists():
            response = read(slot / 'selector_response.json') if (slot / 'selector_response.json').exists() else {}
            raw = response.get('raw', '')
            write(slot / 'selection.json', {'ok': False, 'raw': raw, 'catalog': catalog(skills),
                  'skill_id': parse_selection(raw, [s.skill_id for s in skills]), 'why': parse_why(raw),
                  'load_stage': 'not_loaded', 'error_type': type(exc).__name__})
    try:
        if record.get('trajectory_path') and Path(record['trajectory_path']).exists():
            trace = read(record['trajectory_path'])
            events_path = Path(record['trajectory_path']).parent / 'loading_access_events.json'
            events = read(events_path) if events_path.exists() else []
            evidence = collect_loading_evidence(trace, events, read(slot / 'load_manifest.json'))
            write(slot / 'loading_evidence.json', evidence)
            record.update(read_skill_ids=evidence['read_skill_ids'], rounds=len(trace.get('steps', [])),
                          metrics=trace.get('final_metrics'),
                          tool_calls=sum(len(s.get('tool_calls') or []) for s in trace.get('steps', [])))
    except Exception as exc:
        record['loading_evidence_error'] = safe_error(exc)
    record['duration_seconds'] = time.time() - started
    write(slot / 'record.json', record)
    print(json.dumps({'slot': str(slot.relative_to(ROOT)), 'status': record['status'], 'reward': record['reward']}), flush=True)
    return record


def worker():
    verify_freeze()
    manifest = read(ROOT / 'manifest.json')
    relay = relay_from_env()
    relay.start()
    try:
        with ThreadPoolExecutor(max_workers=manifest['workers']) as pool:
            futures = [pool.submit(execute, row, relay.transport) for row in read(ROOT / 'frozen/schedule.json')]
            for future in as_completed(futures):
                future.result()
        write(ROOT / 'status.json', {'status': 'finished_pending_audit', 'stage': manifest['stage'],
                                    'pid': os.getpid(), 'workers': 0})
    except Exception as exc:
        write(ROOT / 'status.json', {'status': 'needs_attention', 'stage': manifest['stage'],
                                    'error': safe_error(exc), 'workers': 0})
        raise
    finally:
        relay.close()


def launch():
    verify_freeze()
    if (ROOT / 'dispatch.json').exists():
        raise RuntimeError('Already dispatched; no implicit restart')
    env = dict(os.environ)
    for key in ('TMPDIR', 'TEMP', 'TMP'):
        env[key] = str(ROOT / 'tmp')
    env.update(XDG_CACHE_HOME=str(ROOT / 'cache'), PIP_CACHE_DIR=str(ROOT / 'cache/pip'),
               UV_CACHE_DIR=str(ROOT / 'cache/uv'), PYTHONPYCACHEPREFIX=str(ROOT / 'cache/pycache'),
               OPENAI_API_KEY=env['TB21_EXECUTOR_API_KEY'])
    with (ROOT / 'driver.log').open('a') as stream:
        process = subprocess.Popen([sys.executable, str(ROOT / 'runner.py'), '--worker'],
            stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    write(ROOT / 'dispatch.json', {'pid': process.pid, 'at': time.time(), 'no_restart': True})
    write(ROOT / 'status.json', {'status': 'running', 'stage': read(ROOT / 'manifest.json')['stage'],
                                'pid': process.pid, 'workers': read(ROOT / 'manifest.json')['workers']})
    print(json.dumps({'root': str(ROOT), 'pid': process.pid}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', action='store_true')
    worker() if parser.parse_args().worker else launch()
