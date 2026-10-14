"""Single-shot library selection, shared package evolution and frozen evaluation."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / 'code'), str(ROOT)]
from skillexpand import schema as S
from skillexpand.evaluation.selector import parse_selection, parse_why
from skillexpand.l2.editor import SkillEditor
from skillexpand.runtime.progressive import catalog, select_and_load
from skillexpand.runtime.llm_relay import relay_from_env, coalesce_sse
from skillexpand.skill_package import materialize_skill, entrypoint_content
from summarize_tb21_empirical import validate_trial
from input_filter import project_tool_results
from runtime import build_config


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f'.{os.getpid()}.{threading.get_ident()}.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def safe_error(exc):
    text = str(exc)
    for key in ('OPENAI_API_KEY', 'E2B_API_KEY', 'TB21_EXECUTOR_API_KEY', 'TB21_METHOD_API_KEY'):
        value = os.environ.get(key)
        if value:
            text = text.replace(value, '[REDACTED]')
    return text[-3000:]


def verify_freeze():
    if (ROOT / 'integrity_abort.json').exists():
        raise ValueError('Global integrity failure blocks new calls')
    for binding in read(ROOT / 'bindings.json'):
        if (ROOT / binding['current']).read_bytes() != (ROOT / binding['snapshot']).read_bytes():
            raise ValueError('Frozen artifact drift: ' + binding['current'])


def reserve(directory, kind):
    verify_freeze()
    if shutil.disk_usage(ROOT).free < 20 * 1024**3:
        raise OSError('Less than 20 GiB available on the experiment filesystem')
    directory = Path(directory)
    parts = directory.relative_to(ROOT).parts
    panel = read(ROOT / 'frozen/panel.json')
    if kind == 'executor':
        baseline_or_final = (len(parts) == 4 and parts[0] in ('baseline', 'final')
                             and parts[1] == 'slots' and parts[2] in panel['development'] + panel['regression']
                             and parts[3] in ('01', '02', '03'))
        development = (len(parts) == 6 and parts[0] == 'candidates' and parts[1] in ('001', '002')
                       and parts[2:4] == ('dev', 'slots') and parts[4] in panel['development']
                       and parts[5] in ('01', '02'))
        if not baseline_or_final and not development:
            raise ValueError('Executor slot outside frozen panel schedule')
    elif kind == 'generation':
        if not (len(parts) == 4 and parts[0] == 'candidates' and parts[1] in ('001', '002')
                and parts[2] == 'generation' and parts[3] in panel['editable_skills']):
            raise ValueError('Generation slot outside frozen library schedule')
    else:
        raise ValueError('Unknown reservation kind')
    with (ROOT / '.slots.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (directory / 'reserved.json').exists():
            return False
        count = sum(read(p).get('kind') == kind for p in ROOT.rglob('reserved.json'))
        maximum = 62 if kind == 'executor' else 6
        if count >= maximum:
            raise ValueError('Predeclared slot budget exhausted')
        write(directory / 'reserved.json', {'kind': kind,
              'slot': str(directory.relative_to(ROOT)), 'at': time.time(), 'no_retry': True})
        return True


class SelectorHost:
    benchmark_name = 'terminalbench'

    def __init__(self, transport, slot):
        self.transport, self.slot = transport, slot

    def llm(self, messages, **kwargs):
        settings = read(ROOT / 'frozen/selector_settings.json')
        extra_body = settings.pop('extra_body', {})
        payload = {**settings, **extra_body,
                   'messages': [{'role': {'human': 'user', 'ai': 'assistant'}.get(m.type, m.type),
                                 'content': m.content} for m in messages]}
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
            write(self.slot / 'selector_response.json', {'raw': '', 'status': 'failed',
                  'error_type': type(exc).__name__, 'error': safe_error(exc)})
            raise


def claim_identity(slot, record):
    identity = record.get('sandbox_id')
    with (ROOT / '.identities.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ledger_path = ROOT / 'identity_ledger.json'
        ledger = read(ledger_path) if ledger_path.exists() else {}
        excluded = read(ROOT / 'frozen/historical_sandbox_ids.json')
        if not identity:
            raise ValueError('Missing authoritative new sandbox identity')
        if identity in ledger or identity in excluded:
            write(ROOT / 'integrity_abort.json', {'reason': 'duplicate_or_historical_identity',
                  'sandbox_id': identity, 'slot': str(slot.relative_to(ROOT))})
            raise ValueError('Duplicate or historical sandbox identity')
        ledger[identity] = str(slot.relative_to(ROOT))
        write(ledger_path, ledger)


def execute(slot, task, phase, library, transport):
    if not reserve(slot, 'executor'):
        if (slot / 'record.json').exists():
            return read(slot / 'record.json')
        write(slot / 'record.json', {'status': 'unresolved', 'reward': None,
              'task': task['task_name'], 'phase': phase, 'no_retry': True,
              'error_type': 'InterruptedConsumedSlot'})
        return read(slot / 'record.json')
    started = time.time()
    record = {'task': task['task_name'], 'phase': phase, 'status': 'unresolved',
              'reward': None, 'no_retry': True}
    write(slot / 'input.json', {'task': task, 'phase': phase, 'library': library})
    skills = [S.from_dict(S.Skill, value) for value in library]
    write(slot / 'catalog.json', catalog(skills))
    try:
        skill, selection = select_and_load(SelectorHost(transport, slot), task['instruction'], skills)
        selection['raw_skill_lines'] = sum(line.strip().startswith('SKILL:')
                                          for line in selection['raw'].splitlines())
        write(slot / 'selection.json', selection)
        loaded = next(value for value in library if value['skill_id'] == skill.skill_id)
        write(slot / 'loaded_skill.json', loaded)
        record.update(selected_skill_id=skill.skill_id, loaded_skill_key=skill.key)
        package = slot / 'skills' / task['task_name']
        entry = materialize_skill(skill, package, package=skill.package_mode)
        files = {str(p.relative_to(package)): p.read_text()
                 for p in sorted(package.rglob('*')) if p.is_file()}
        write(slot / 'load_manifest.json', {'source_path': str(entry),
              'skill_path': f"/opt/openclaw-skills/{task['task_name']}/SKILL.md", 'files': files,
              'load_stage': 'after_selection', 'loaded_skill_id': skill.skill_id})
        config = build_config(read(ROOT / 'frozen/executor_template.json'), ROOT, slot,
                              task['task_name'], slot / 'skills')
        write(slot / 'harbor_config.json', config)
        env = dict(os.environ)
        env.update(OPENAI_API_KEY=env['TB21_EXECUTOR_API_KEY'],
                   PYTHONPATH=':'.join([str(ROOT), str(ROOT / 'frozen/runtime/tencent/python'),
                                       str(ROOT / 'frozen/runtime/framework')]))
        with (slot / 'harbor.log').open('a') as stream:
            completed = subprocess.run([read(ROOT / 'manifest.json')['harbor_python'],
                 str(ROOT / 'runtime.py'), '--config', str(slot / 'harbor_config.json'),
                 '--retry-archive-root', str(slot / 'retry_archives'), '--no-export-traces'],
                 env=env, stdout=stream, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        record['harbor_returncode'] = completed.returncode
        results = list((slot / 'jobs').glob('*/*/result.json'))
        if len(results) != 1:
            raise ValueError('Missing or duplicate official trial')
        result_path = results[0]
        record.update(result_path=str(result_path),
                      trajectory_path=str(result_path.parent / 'agent/trajectory.json'))
        snapshot = read(result_path.parent / 'snapshot.json')
        record['sandbox_id'] = snapshot.get('source_sandbox_id')
        claim_identity(slot, record)
        result, reward, trace_path = validate_trial(result_path, task['task_name'], activation=True)
        actual = result['config']['agent']
        expected = deepcopy(config['agents'][0])
        actual = {**actual, 'name': expected.get('name')}
        if actual != expected:
            raise ValueError('Official executor configuration drift')
        effective = read(result_path.parent / 'agent/effective_agent_config.json')
        if effective.get('max_input_tokens') != 262144 or effective.get('max_output_tokens') != 65536:
            raise ValueError('Effective context drift')
        if not effective.get('enable_summarize') or effective.get('proactive_summarization_threshold') != 8000:
            raise ValueError('Effective summarization drift')
        activation = read(result_path.parent / 'agent/skill_activation.json')
        if Path(activation['source_path']).resolve() != entry.resolve():
            raise ValueError('Selected package mount path drift')
        if entry.read_text() != entrypoint_content(skill, package=skill.package_mode):
            raise ValueError('Selected package entrypoint drift')
        if files != {str(p.relative_to(package)): p.read_text()
                     for p in package.rglob('*') if p.is_file()}:
            raise ValueError('Selected resource bytes drift')
        trace = read(trace_path)
        record.update(status='valid', reward=reward, metrics=trace.get('final_metrics'),
                      rounds=len(trace['steps']),
                      tool_calls=sum(len(s.get('tool_calls') or []) for s in trace['steps']),
                      exception=result.get('exception_info'))
    except Exception as exc:
        record.update(status='unresolved', reward=None, error_type=type(exc).__name__, error=safe_error(exc))
        if not (slot / 'selection.json').exists():
            response = read(slot / 'selector_response.json') if (slot / 'selector_response.json').exists() else {}
            raw = response.get('raw', '')
            write(slot / 'selection.json', {'ok': False, 'raw': raw, 'catalog': catalog(skills),
                  'skill_id': parse_selection(raw, [s.skill_id for s in skills]), 'why': parse_why(raw),
                  'load_stage': 'not_loaded', 'error_type': type(exc).__name__})
    record['duration_seconds'] = time.time() - started
    write(slot / 'record.json', record)
    print(json.dumps({'slot': str(slot.relative_to(ROOT)), 'status': record['status'],
                      'reward': record['reward'], 'selected': record.get('selected_skill_id')}), flush=True)
    return record


def stage(directory, phase, library, tasks, attempts, transport):
    verify_freeze()
    summary = directory / 'stage_summary.json'
    if summary.exists():
        return read(summary)
    write(ROOT / 'status.json', {'status': 'running', 'stage': 'library_pilot', 'phase': phase,
          'pid': os.getpid(), 'workers': 21, 'at': time.time()})
    slots = [(directory / 'slots' / task['task_name'] / f'{attempt:02d}', task)
             for task in tasks for attempt in range(1, attempts + 1)]
    records = []
    with ThreadPoolExecutor(max_workers=21) as pool:
        futures = [pool.submit(execute, slot, task, phase, library, transport) for slot, task in slots]
        for future in as_completed(futures):
            records.append(future.result())
    rows = {'phase': phase, 'slots': len(slots), 'valid': sum(r['status'] == 'valid' for r in records),
            'successes': sum(r['status'] == 'valid' and r['reward'] == 1 for r in records),
            'unresolved': sum(r['status'] != 'valid' for r in records)}
    write(summary, rows)
    return rows


def evidence_for(skill_id, directories, task_table):
    eligible = {task['task_name']: task for task in task_table if task['split'] == 'development'}
    rows, contracts, paths = [], {}, []
    for directory in directories:
        for path in sorted((directory / 'slots').glob('*/*/record.json')):
            row = read(path)
            task_name = row['task']
            if (task_name not in eligible or row.get('selected_skill_id') != skill_id
                    or row.get('phase') == 'final'):
                continue
            slot = path.parent
            trace_path = Path(row.get('trajectory_path') or '__missing__')
            observations, trace_status = [], 'missing'
            if trace_path.exists():
                try:
                    observations = project_tool_results(read(trace_path))
                    trace_status = 'complete'
                except (ValueError, OSError, UnicodeError):
                    trace_status = 'damaged_not_used_for_generation'
            relative = str(slot.relative_to(ROOT))
            rows.append({'id': relative, 'task_name': task_name, 'phase': row['phase'],
                         'selected_skill_id': skill_id, 'loaded_skill_key': row['loaded_skill_key'],
                         'loaded_skill': read(slot / 'loaded_skill.json'),
                         'official_reward': row['reward'] if row['status'] == 'valid' else None,
                         'tool_observations': observations, 'trace_status': trace_status})
            paths.append(relative)
            contracts[task_name] = eligible[task_name]['instruction']
    return {'public_contracts': [{'task_name': k, 'public_contract': v} for k, v in sorted(contracts.items())],
            'historical_evidence': rows,
            'evidence_status': 'Current pilot development only, grouped by actual model selection; '
                               'unknown rewards remain null; restricted tool steps masked; legal observations '
                               'are full length with no per-entry or total projection budget.'}, paths


def candidate(number, current, sources, tasks):
    directory = ROOT / 'candidates' / f'{number:03d}'
    if (directory / 'candidate_status.json').exists():
        return read(directory / 'library.json'), read(directory / 'candidate_status.json')
    values = deepcopy(current)
    changed = []
    relay = None
    try:
        for index, raw in enumerate(current):
            skill_id = raw['skill_id']
            output = directory / 'generation' / skill_id
            evidence, paths = evidence_for(skill_id, sources, tasks)
            generation_input = {'base_skill': raw, 'evidence': evidence, 'feedback': [], 'source_slots': paths}
            if not paths:
                write(output / 'generation_input.json', generation_input)
                write(output / 'generation_status.json', {'status': 'skipped_no_selected_evidence', 'no_retry': True})
                continue
            if not reserve(output, 'generation'):
                if (output / 'skill.json').exists():
                    values[index] = read(output / 'skill.json')
                    changed.append(skill_id)
                continue
            write(output / 'generation_input.json', generation_input)
            started = time.time()
            base = S.from_dict(S.Skill, raw)
            try:
                editor = SkillEditor(SimpleNamespace(llm=None), SimpleNamespace(version=0))
                messages = editor.build_package_prompt(base, evidence)
                payload = {**read(ROOT / 'frozen/generation_settings.json'),
                           'messages': [{'role': 'system' if m.type == 'system' else 'user',
                                         'content': m.content} for m in messages]}
                write(output / 'request.json', payload)
                if relay is None:
                    os.environ['OPENAI_API_KEY'] = os.environ['TB21_METHOD_API_KEY']
                    relay = relay_from_env()
                    relay.start()
                with (output / 'response.sse').open('wb') as stream:
                    def chunks():
                        for chunk in relay.transport.stream(payload):
                            chunk = chunk.encode() if isinstance(chunk, str) else chunk
                            stream.write(chunk)
                            stream.flush()
                            yield chunk
                    completion = coalesce_sse(chunks())
                write(output / 'completion.json', completion)
                text = completion['choices'][0]['message']['content']
                editor = SkillEditor(SimpleNamespace(llm=lambda *a, **kw: text), SimpleNamespace(version=0))
                outcome = editor.propose_package(base, evidence)
                write(output / 'outcome.json', S.to_dict(outcome))
                if outcome.proposed:
                    proposed = S.to_dict(outcome.candidate.skill)
                    for key in ('skill_id', 'family_id', 'name', 'description'):
                        if proposed[key] != raw[key]:
                            raise ValueError('Routing metadata changed')
                    values[index] = proposed
                    changed.append(skill_id)
                    write(output / 'skill.json', proposed)
                write(output / 'generation_status.json', {'status': 'generated' if outcome.proposed else outcome.reason,
                      'seconds': time.time() - started, 'no_retry': True})
            except Exception as exc:
                write(output / 'generation_status.json', {'status': 'failed', 'error_type': type(exc).__name__,
                      'error': safe_error(exc), 'seconds': time.time() - started, 'no_retry': True})
    finally:
        if relay is not None:
            relay.close()
        os.environ['OPENAI_API_KEY'] = os.environ['TB21_EXECUTOR_API_KEY']
    verify_freeze()
    status = {'number': number, 'usable': bool(changed), 'changed_skills': changed,
              'status': 'generated' if changed else 'no_usable_change'}
    write(directory / 'library.json', values)
    write(directory / 'candidate_status.json', status)
    return values, status


def worker():
    verify_freeze()
    tasks = read(ROOT / 'frozen/task_table.json')
    development = [task for task in tasks if task['split'] == 'development']
    seed = read(ROOT / 'frozen/seed_library.json')
    relay = None
    try:
        os.environ['OPENAI_API_KEY'] = os.environ['TB21_EXECUTOR_API_KEY']
        relay = relay_from_env()
        relay.start()
        stage(ROOT / 'baseline', 'baseline', seed, tasks, 3, relay.transport)
        current, sources, choices = seed, [ROOT / 'baseline'], []
        for number in (1, 2):
            write(ROOT / 'status.json', {'status': 'running', 'phase': f'candidate_{number}_generation',
                  'pid': os.getpid(), 'workers': 21, 'stage': 'library_pilot'})
            library, status = candidate(number, current, sources, tasks)
            if status['usable']:
                directory = ROOT / 'candidates' / f'{number:03d}'
                result = stage(directory / 'dev', f'candidate_{number}_development',
                               library, development, 2, relay.transport)
                choices.append({'number': number, 'successes': result['successes']})
                current, sources = library, [ROOT / 'baseline', directory / 'dev']
        selected = min(choices, key=lambda c: (-c['successes'], c['number']))['number'] if choices else None
        selection_path = ROOT / 'final/selection.json'
        selection = {'selected': selected, 'rule': 'max_official_development_successes_tie_earlier',
                     'development_scores': choices, 'final_feedback_used': False}
        if selection_path.exists() and read(selection_path) != selection:
            raise ValueError('Frozen library selection drift')
        write(selection_path, selection)
        if selected is not None:
            library = read(ROOT / 'candidates' / f'{selected:03d}' / 'library.json')
            final_path = ROOT / 'final/library.json'
            if final_path.exists() and read(final_path) != library:
                raise ValueError('Frozen final library drift')
            write(final_path, library)
            stage(ROOT / 'final', 'final', library, tasks, 3, relay.transport)
        write(ROOT / 'status.json', {'status': 'finished_pending_audit', 'phase': 'protocol_finished',
              'pid': os.getpid(), 'workers': 0, 'stage': 'library_pilot'})
    except Exception as exc:
        write(ROOT / 'status.json', {'status': 'needs_attention', 'phase': 'failed',
              'pid': os.getpid(), 'workers': 0, 'error_type': type(exc).__name__, 'error': safe_error(exc)})
        raise
    finally:
        if relay is not None:
            relay.close()


def launch():
    verify_freeze()
    manifest = read(ROOT / 'manifest.json')
    runs = ROOT.parent
    with (runs / '.tb21-worker-reservations.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (ROOT / 'dispatch.json').exists():
            raise ValueError('Already dispatched; consumed calls cannot be replaced')
        reserved = 0
        for path in runs.rglob('status.json'):
            if path.parent == ROOT:
                continue
            try:
                value = read(path)
            except (ValueError, OSError):
                continue
            pid = value.get('pid')
            if value.get('status') != 'running' or not pid:
                continue
            try:
                alive = (Path('/proc') / str(pid) / 'stat').read_text().split(') ', 1)[1].split()[0] != 'Z'
            except OSError:
                alive = False
            if alive:
                if not isinstance(value.get('workers'), int):
                    raise ValueError('Active experiment has unknown reserved workers')
                reserved += value['workers']
        if reserved + 21 > 250:
            raise ValueError('Shared worker capacity unavailable')
        env = dict(os.environ)
        for name, relative in {'TMPDIR': 'tmp', 'TEMP': 'tmp', 'TMP': 'tmp',
                               'XDG_CACHE_HOME': 'cache', 'PIP_CACHE_DIR': 'cache/pip',
                               'UV_CACHE_DIR': 'cache/uv', 'PYTHONPYCACHEPREFIX': 'cache/pycache'}.items():
            path = ROOT / relative
            path.mkdir(parents=True, exist_ok=True)
            env[name] = str(path)
        env.update(TBENCH_PERSIST_SANDBOXES='0', TBENCH_TENCENT_ENV_FILE='/dev/null',
                   E2B_DOMAIN='ap-beijing.tencentags.com', E2B_VALIDATE_API_KEY='false',
                   HARBOR_E2B_FS_USER='root', TB21_PACKAGE_DNS='183.60.83.19',
                   TBENCH_E2B_RELAY_TEMPLATE='sdt-28lb4d9k',
                   TBENCH_E2B_RELAY_USE_TEMPLATE_DEFAULT='1',
                   TBENCH_E2B_RELAY_METADATA_JSON='{"x-mounts":"[]"}',
                   TBENCH_RELAY_PROVIDER_BASE='https://llm-center.modelbest.co/v1',
                   EXPE_LLM_TIMEOUT_SECONDS='1800', TBENCH_E2B_START_CONCURRENCY='4',
                   TBENCH_E2B_RPC_CONCURRENCY='12', TB21_TOTAL_WORKER_LIMIT='250',
                   TB21_PIPELINE_CACHE='0', TB21_TASK_VERIFIER_CACHES='{}',
                   PYTHONPATH=str(ROOT / 'code'))
        paths = {name: {'path': env[name], 'resolved': str(Path(env[name]).resolve()),
                       'free_bytes': shutil.disk_usage(env[name]).free}
                 for name in ('TMPDIR', 'XDG_CACHE_HOME', 'PIP_CACHE_DIR', 'UV_CACHE_DIR', 'PYTHONPYCACHEPREFIX')}
        for name, info in paths.items():
            if not Path(info['resolved']).is_relative_to(Path('/data2')):
                raise ValueError('Writable runtime path not under data2: ' + name)
        probe = ROOT / 'tmp/write_probe.json'
        write(probe, {'durable_write': True, 'at': time.time()})
        with (ROOT / 'driver.log').open('a') as stream:
            child = subprocess.Popen([sys.executable, str(ROOT / 'runner.py'), '--worker'],
                  env=env, stdout=stream, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                  start_new_session=True)
        write(ROOT / 'dispatch.json', {'pid': child.pid, 'at': time.time(), 'workers': 21,
              'prior_reserved': reserved, 'writable_paths': paths, 'budget_reset': False,
              'protocol_revision': manifest['protocol_revision']})
        write(ROOT / 'status.json', {'status': 'running', 'phase': 'launching', 'stage': 'library_pilot',
              'pid': child.pid, 'workers': 21})
        print(json.dumps({'pid': child.pid, 'root': str(ROOT), 'workers': 21, 'planned_executor': 62}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', action='store_true')
    args = parser.parse_args()
    worker() if args.worker else launch()
