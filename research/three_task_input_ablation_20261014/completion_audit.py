"""Audit saved experiment artifacts only; never launch a model or evaluator.

Run with the research runtime or standard Python. Exact native prompt/proposal
methods are replayed from frozen AST nodes with a recorded completion as the LLM
return value. The only message adapter supplies .type/.content; no proposal or
parsing semantics are reimplemented. Native provenance creation timestamps are
compared separately because replay occurs at a later time.
"""
import argparse
import ast
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace


DEFAULT_ROOT = Path(__file__).resolve().parent
DEFAULT_REMOTE_ROOT = '/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/three-task-input-ablation-20261014'
TASKS = ('cancel-async-tasks', 'multi-source-data-merger', 'log-summary-date-ranges')
ARMS = ('A', 'B')
TOKEN_KEYS = {
    'input': ('total_prompt_tokens', 'prompt_tokens', 'input_tokens', 'total_input_tokens', 'n_input_tokens'),
    'output': ('total_completion_tokens', 'completion_tokens', 'output_tokens', 'total_output_tokens', 'n_output_tokens'),
    'cached': ('total_cached_tokens', 'cached_tokens', 'cache_tokens'),
    'total': ('total_tokens',),
}


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def tokens(metrics):
    """Keep measured values and their source names; never invent token costs."""
    metrics = metrics if isinstance(metrics, dict) else {}
    values, sources = {}, {}
    for label, names in TOKEN_KEYS.items():
        key = next((k for k in names if isinstance(metrics.get(k), (int, float))
                    and not isinstance(metrics[k], bool)), None)
        values[label] = metrics.get(key) if key else None
        sources[label] = key
    if values['total'] is None and all(values[k] is not None for k in ('input', 'output')):
        values['total'] = values['input'] + values['output']
        sources['total'] = 'derived_input_plus_output'
    return {'values': values, 'source_keys': sources, 'available_metric_keys': sorted(metrics)}


def aggregate_tokens(rows):
    result = {}
    for key in TOKEN_KEYS:
        known = [r['tokens']['values'][key] for r in rows if r.get('tokens', {}).get('values', {}).get(key) is not None]
        result[key] = {'observed_sum': sum(known) if known else None,
                       'known_records': len(known), 'unknown_records': len(rows)-len(known),
                       'complete': bool(rows) and len(known) == len(rows)}
    return result


@dataclass
class AuditMessage:
    content: str
    type: str


class NativeReplay:
    def __init__(self, root):
        # These modules are standard-library-only and are included in bindings.
        sys.path.insert(0, str(root/'code'))
        from skillexpand import schema
        from skillexpand.l1.family_discovery import _extract_json
        from skillexpand.skill_package import entrypoint_content
        self.schema = schema
        self.entrypoint_content = entrypoint_content
        self.validator = load_module('_completion_official_validator', root/'code/summarize_tb21_empirical.py')
        self.filter = load_module('_completion_fixed_filter', root/'input_filter.py')
        self.relay = load_module('_completion_sse', root/'code/skillexpand/runtime/llm_relay.py')
        tree = ast.parse((root/'code/skillexpand/l2/editor.py').read_text())
        nodes = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and
                    t.id in {'EXISTING_RULE_CHECK', 'REASON_PROPOSED', 'REASON_NO_OPERATIONS', 'REASON_NO_CHANGE'}
                    for t in node.targets):
                nodes.append(node)
            elif isinstance(node, ast.ClassDef) and node.name == 'EditOutcome':
                nodes.append(node)
            elif isinstance(node, ast.ClassDef) and node.name == 'SkillEditor':
                node.body = [n for n in node.body if isinstance(n, ast.FunctionDef)
                             and n.name in {'__init__', 'build_package_prompt', 'propose_package'}]
                nodes.append(node)
        namespace = {'dataclass': dataclass, 'S': schema, 'json': json, '_extract_json': _extract_json,
                     'SystemMessage': lambda content: AuditMessage(content, 'system'),
                     'HumanMessage': lambda content: AuditMessage(content, 'human')}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(root/'code/skillexpand/l2/editor.py'), 'exec'), namespace)
        self.editor_class = namespace['SkillEditor']

    def request(self, base, evidence, feedback, settings):
        editor = self.editor_class(None, SimpleNamespace(version=0))
        messages = editor.build_package_prompt(self.schema.from_dict(self.schema.Skill, base), evidence, feedback)
        return {**settings, 'messages': [{'role': 'system' if m.type == 'system' else 'user', 'content': m.content}
                                         for m in messages]}

    def outcome(self, base, evidence, feedback, completion):
        editor = self.editor_class(SimpleNamespace(llm=lambda *a, **kw: completion), SimpleNamespace(version=0))
        return self.schema.to_dict(editor.propose_package(self.schema.from_dict(self.schema.Skill, base), evidence, feedback))


def without_creation_time(value):
    """Only native provenance's wall-clock timestamp is non-deterministic."""
    if isinstance(value, dict):
        return {k: ({pk: without_creation_time(pv) for pk, pv in v.items() if pk != 'created_at'}
                    if k == 'provenance' and isinstance(v, dict) else without_creation_time(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [without_creation_time(v) for v in value]
    return value


class CompletionAudit:
    def __init__(self, root=DEFAULT_ROOT, remote_root=DEFAULT_REMOTE_ROOT):
        self.root = Path(root).resolve()
        self.remote = Path(remote_root)
        self.native = NativeReplay(self.root)
        self.errors, self.pending, self.trials, self.generations, self.arms = [], [], {}, [], []
        self.identities = defaultdict(list)
        self.recoveries, self.recovered_paths, self.recovery_archives, self.source_limits = {}, {}, set(), []
        self.allowed_executor, self.allowed_generation = set(), set()
        self.settings = read(self.root/'frozen/generation_settings.json')
        self.excluded = set(read(self.root/'frozen/historical_sandbox_ids.json'))

    def map_path(self, value):
        path = Path(value)
        if path.is_relative_to(self.remote):
            return self.root/path.relative_to(self.remote)
        return path if path.is_absolute() else self.root/path

    def normalized(self, value):
        if isinstance(value, str) and value.startswith(str(self.remote)+'/'):
            return str(self.map_path(value))
        if isinstance(value, dict):
            return {k: self.normalized(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.normalized(v) for v in value]
        return value

    def error(self, path, code):
        item = {'path': str(path.relative_to(self.root)) if isinstance(path, Path) and path.is_relative_to(self.root) else str(path),
                'code': code}
        if item not in self.errors:
            self.errors.append(item)

    def wait(self, path, code):
        self.pending.append({'path': str(path.relative_to(self.root)), 'code': code})

    def same(self, path, actual, expected, code):
        if self.normalized(actual) != self.normalized(expected):
            self.error(path, code)
            return False
        return True

    def read_optional(self, path):
        if not path.exists():
            return None
        try:
            return read(path)
        except (ValueError, OSError, TypeError):
            if path in self.recovered_paths:
                self.source_limit(path, 'documented_disk_failure_unreadable_json')
            else:
                self.error(path, 'malformed_or_unreadable_json')
            return None

    def source_limit(self, path, code):
        item = {'path': str(path.relative_to(self.root)), 'code': code,
                'reward_credit': None, 'evidence_status': 'infrastructure_source_artifact_lost_or_incomplete'}
        if item not in self.source_limits:
            self.source_limits.append(item)

    def check_recoveries(self):
        """Recovery is supplementary evidence; it never repairs official rewards."""
        for marker in (self.root/'tasks').rglob('infra_recovery.json'):
            if 'frozen' in marker.relative_to(self.root).parts:
                continue
            slot = marker.parent
            try:
                recovery = read(marker)
                before = len(self.errors)
                if recovery.get('reason') != 'host_disk_write_failure' or recovery.get('no_retry') is not True:
                    self.error(marker, 'invalid_infrastructure_recovery_reason_or_retry_policy')
                if not (slot/'reserved.json').exists():
                    self.error(marker, 'infrastructure_recovery_without_consumed_slot')
                checked = recovery.get('checked_at', recovery.get('checked_atUTC'))
                if not isinstance(checked, str):
                    self.error(marker, 'recovery_missing_process_check_timestamp')
                else:
                    datetime.fromisoformat(checked.replace('Z', '+00:00'))
                processes = recovery.get('terminal_processes', [])
                if not processes or any(not isinstance(p.get('pid'), int) or p.get('alive') is not False
                                        or not p.get('evidence') for p in processes):
                    self.error(marker, 'recovery_missing_terminal_process_gone_evidence')
                preserved = recovery.get('preserved_files', [])
                if not preserved:
                    self.error(marker, 'recovery_missing_preserved_source_bytes')
                paths = []
                for entry in preserved:
                    original = self.root/entry['original'];archive = self.root/entry['archive']
                    if (not original.is_relative_to(slot) or not archive.is_relative_to(slot)
                            or '..' in Path(entry['original']).parts or '..' in Path(entry['archive']).parts
                            or original == archive or archive.is_symlink()):
                        self.error(marker, 'recovery_archive_path_invalid')
                        continue
                    data = archive.read_bytes()
                    self.recovery_archives.add(archive)
                    if len(data) != entry['bytes']:
                        self.error(marker, 'recovery_archive_byte_count_mismatch')
                    replaced = entry.get('replaced', entry.get('original_replaced', False))
                    permitted = original.name == 'record.json' and recovery.get('record_recovered') is True
                    permitted = permitted or original.name == 'generation_status.json'
                    permitted = permitted or original.name in {'completion.json', 'outcome.json', 'generation_metrics.json', 'skill.json'} and recovery.get('generation_recovered') is True
                    if replaced:
                        if not permitted:
                            self.error(marker, 'recovery_replaced_authoritative_source_not_permitted')
                        self.source_limit(original, 'original_wrapper_bytes_archived_before_declared_reconstruction')
                    elif original.read_bytes() != data:
                        self.error(marker, 'recovery_archive_not_exact_original_bytes')
                    paths.append(original)
                if len(self.errors) == before:
                    self.recoveries[slot] = recovery
                    for path in paths:
                        self.recovered_paths[path] = slot
                    self.source_limit(marker, 'documented_consumed_slot_disk_failure_no_retry')
            except (KeyError, TypeError, ValueError, OSError):
                self.error(marker, 'invalid_infrastructure_recovery_artifact')

    def check_freeze(self):
        bindings = read(self.root/'bindings.json')
        if not bindings:
            self.error(self.root/'bindings.json', 'empty_bindings')
        for row in bindings:
            current, snapshot = self.root/row['current'], self.root/row['snapshot']
            try:
                if current.resolve() == snapshot.resolve() or current.read_bytes() != snapshot.read_bytes():
                    self.error(current, 'frozen_bytes_changed')
            except OSError:
                self.error(current, 'frozen_binding_missing')
        manifest = read(self.root/'manifest.json')
        self.same(self.root/'manifest.json', manifest['tasks'], list(TASKS), 'task_set_changed')
        self.same(self.root/'manifest.json', manifest['arms'], list(ARMS), 'arm_set_changed')
        self.same(self.root/'manifest.json', manifest['budget'], {
            'baseline_per_task': 5, 'candidates_per_arm': 2, 'dev_per_candidate': 3,
            'final_per_arm': 5, 'max_executor_trials': 81, 'generation_slots': 12}, 'budget_changed')
        if manifest.get('preaudit_passed') is not True:
            self.error(self.root/'manifest.json', 'missing_preaudit')
        if (self.root/'integrity_abort.json').exists():
            self.error(self.root/'integrity_abort.json', 'global_identity_abort')
        return len(bindings)

    def inventory(self):
        for task in TASKS:
            tr = self.root/'tasks'/task
            self.allowed_executor.update(tr/'baseline'/f'{i:02d}' for i in range(1, 6))
            for arm in ARMS:
                ar = tr/'arms'/arm
                for number in (1, 2):
                    candidate = ar/f'candidate-{number:03d}'
                    self.allowed_generation.add(candidate)
                    self.allowed_executor.update(candidate/'dev'/f'{i:02d}' for i in range(1, 4))
                self.allowed_executor.update(ar/'final'/f'{i:02d}' for i in range(1, 6))
        reserved = []
        for path in (self.root/'tasks').rglob('reserved.json'):
            if 'frozen' in path.relative_to(self.root).parts or path in self.recovery_archives:
                continue
            record = self.read_optional(path)
            if record is None:
                continue
            directory = path.parent
            kind = record.get('kind')
            allowed = self.allowed_executor if kind == 'executor' else self.allowed_generation if kind == 'generation' else set()
            if directory not in allowed:
                self.error(path, 'reservation_outside_fixed_protocol')
            self.same(path, record.get('slot'), str(directory.relative_to(self.root)), 'reservation_slot_mismatch')
            reserved.append({'path': str(directory.relative_to(self.root)), 'kind': kind})
        counts = Counter(r['kind'] for r in reserved)
        if counts['executor'] > 81 or counts['generation'] > 12:
            self.error(self.root/'tasks', 'reservation_budget_exceeded')
        for name in ('record.json', 'started.json', 'generation_status.json', 'completion.json', 'response.sse'):
            for path in (self.root/'tasks').rglob(name):
                if 'frozen' in path.relative_to(self.root).parts or path in self.recovery_archives:
                    continue
                allowed = self.allowed_executor if name in ('record.json', 'started.json') else self.allowed_generation
                if path.parent not in allowed:
                    self.error(path, 'execution_or_generation_artifact_outside_fixed_slot')
                if not (path.parent/'reserved.json').exists():
                    self.error(path, 'artifact_without_reservation')
        for name in ('snapshot.json', 'result.json', 'config.json'):
            for path in (self.root/'tasks').rglob(name):
                if 'frozen' in path.relative_to(self.root).parts or 'jobs' not in path.relative_to(self.root).parts or path in self.recovery_archives:
                    continue
                parts = path.relative_to(self.root).parts
                slot = self.root/Path(*parts[:parts.index('jobs')])
                if slot not in self.allowed_executor or not (slot/'reserved.json').exists():
                    self.error(path, 'official_execution_artifact_outside_reserved_slot')
        return {'allowed_executor_slots': 81, 'allowed_generation_slots': 12,
                'executor_reserved': counts['executor'], 'generation_reserved': counts['generation'], 'reservations': reserved}

    def snapshot_inventory(self):
        # Read every actual snapshot, including malformed/unresolved results and
        # snapshots from executions whose wrapper never produced record.json.
        for slot in sorted(self.allowed_executor):
            for snapshot in slot.glob('jobs/*/*/snapshot.json'):
                payload = self.read_optional(snapshot)
                identity = payload.get('source_sandbox_id') if payload else None
                if identity:
                    self.identities[identity].append({'slot': str(slot.relative_to(self.root)),
                                                      'snapshot': str(snapshot.relative_to(self.root))})
        for identity, rows in self.identities.items():
            if identity in self.excluded or len(rows) != 1:
                for row in rows:
                    self.error(row['snapshot'], 'sandbox_historical_or_globally_reused')

    def trial(self, slot, task, skill, development=False):
        if slot in self.trials:
            return self.trials[slot]
        row = {'path': str(slot.relative_to(self.root)), 'task': task, 'reserved': (slot/'reserved.json').exists(),
               'finished': False, 'claimed_status': None, 'valid': False, 'reward': None,
               'sandbox_id': None, 'fresh_unique': False, 'tokens': tokens(None), 'outcome_issues': []}
        self.trials[slot] = row
        if not row['reserved']:
            self.wait(slot, 'required_executor_slot_not_reserved')
            return row
        record = self.read_optional(slot/'record.json')
        if record is None:
            self.wait(slot, 'reserved_executor_slot_not_finished')
            return row
        row.update(finished=True, claimed_status=record.get('status'), duration_seconds=record.get('duration_seconds'),
                   error_type=record.get('error_type'))
        recovery = self.recoveries.get(slot)
        if recovery:
            row['infrastructure_recovery'] = {'reason': recovery['reason'],
                'source_artifact_lost': recovery.get('source_artifact_lost', False), 'no_retry': True}
            if recovery.get('record_recovered') and (record.get('status') != 'unresolved' or record.get('reward') is not None):
                self.error(slot/'record.json', 'infrastructure_reconstructed_record_claims_reward_or_validity')
        if record.get('status') not in ('valid', 'unresolved') or record.get('no_retry') is not True:
            self.error(slot/'record.json', 'invalid_slot_status_or_no_retry_marker')
        frozen = self.root/'tasks'/task/'frozen'
        protocol, executor = read(frozen/'protocol.json'), read(frozen/'executor.json')
        expected_input = {'skill': skill, 'executor': executor, 'task_path': str(frozen/'tasks'/task), 'protocol': protocol}
        trial_input = self.read_optional(slot/'input.json')
        if trial_input is not None:
            self.same(slot/'input.json', trial_input, expected_input, 'slot_input_differs_from_frozen_skill_or_settings')
        elif record.get('error_type') != 'InterruptedReservedSlot':
            if recovery and (slot/'input.json') in self.recovered_paths:
                self.source_limit(slot/'input.json', 'actual_slot_input_lost_unknown_configuration')
            else:
                self.error(slot/'input.json', 'missing_actual_slot_input')
        if trial_input is not None and not (slot/'started.json').exists():
            self.error(slot, 'actual_execution_input_without_start_record')
        jobs = sorted(p for p in (slot/'jobs').glob('*/*') if p.is_dir() and '__' in p.name)
        if len(jobs) > 1:
            self.error(slot/'jobs', 'more_than_one_official_trial_or_retry')
        configs = sorted((slot/'jobs').glob('*/config.json'))
        if len(configs) > 1:
            self.error(slot/'jobs', 'more_than_one_harbor_job')
        for config_path in configs:
            config = self.read_optional(config_path)
            if not config:
                continue
            if config.get('n_attempts') != 1 or config.get('orchestrator', {}).get('retry', {}).get('max_retries') != 0:
                self.error(config_path, 'harbor_attempts_or_retry_policy_drift')
            if config.get('orchestrator', {}).get('n_concurrent_trials') != 1:
                self.error(config_path, 'per_slot_harbor_concurrency_drift')
            datasets = config.get('datasets') or []
            if len(datasets) != 1 or datasets[0].get('task_names') != [task]:
                self.error(config_path, 'harbor_task_filter_drift')
            elif not self.same(config_path, datasets[0].get('path'), str(frozen/'tasks'), 'harbor_dataset_path_drift'):
                pass
        result_path = self.map_path(record['result_path']) if record.get('result_path') else None
        if result_path is None and recovery and len(jobs) == 1 and (jobs[0]/'result.json').exists():
            result_path = jobs[0]/'result.json'
        if result_path and not result_path.is_relative_to(slot):
            self.error(slot/'record.json', 'official_result_outside_slot')
            result_path = None
        if result_path and result_path.exists():
            if record.get('trajectory_path') and self.map_path(record['trajectory_path']) != result_path.parent/'agent/trajectory.json':
                self.error(slot/'record.json', 'record_trajectory_differs_from_official_trial')
            if len(jobs) != 1 or result_path.parent != jobs[0] or len(configs) != 1:
                self.error(slot/'record.json', 'official_result_job_or_trial_count_mismatch')
            snapshot = self.read_optional(result_path.parent/'snapshot.json')
            identity = snapshot.get('source_sandbox_id') if snapshot else None
            row['sandbox_id'] = identity
            row['fresh_unique'] = bool(identity) and identity not in self.excluded and len(self.identities.get(identity, [])) == 1
            if record.get('sandbox_id') != identity and not (recovery and recovery.get('record_recovered') and not record.get('sandbox_id')):
                self.error(slot/'record.json', 'record_snapshot_identity_mismatch')
            if record.get('identity_valid') is True and not row['fresh_unique']:
                self.error(slot/'record.json', 'claimed_valid_identity_not_fresh_unique')
            actual = self.read_optional(result_path)
            if actual:
                config = actual.get('config', {})
                for key in ('agent', 'verifier'):
                    self.same(result_path, config.get(key), executor[key], key+'_configuration_drift')
                environment = json.loads(json.dumps(executor['environment']))
                environment['kwargs']['skill_root'] = str(slot/'skills')
                self.same(result_path, config.get('environment'), environment, 'environment_configuration_drift')
                self.same(result_path, config.get('task'), {**executor['task'], 'path': str(frozen/'tasks'/task)}, 'task_configuration_drift')
                for key in ('timeout_multiplier', 'agent_timeout_multiplier', 'verifier_timeout_multiplier',
                            'agent_setup_timeout_multiplier', 'environment_build_timeout_multiplier'):
                    self.same(result_path, config.get(key), executor.get(key), key+'_drift')
            try:
                actual, reward, verified_trace = self.native.validator.validate_trial(result_path, task, activation=True, model=protocol['model_name'])
                row['official_reward'] = reward
                if record.get('status') == 'valid':
                    self.same(slot/'record.json', record.get('reward'), reward, 'claimed_reward_differs_from_official')
                    if isinstance(record.get('reward'), bool):
                        self.error(slot/'record.json', 'boolean_reward')
                row['exception_type'] = (actual.get('exception_info') or {}).get('exception_type')
                activation = read(result_path.parent/'agent/skill_activation.json')
                mounted = self.map_path(activation['source_path'])
                expected_mounted = slot/'skills'/task/'SKILL.md'
                self.same(result_path, str(mounted), str(expected_mounted), 'mounted_source_path_mismatch')
                self.same(result_path, activation.get('skill_path'), '/opt/openclaw-skills/'+task+'/SKILL.md', 'mounted_target_path_mismatch')
                model_skill = self.native.schema.from_dict(self.native.schema.Skill, skill)
                expected_files = {'SKILL.md': self.native.entrypoint_content(model_skill, package=model_skill.package_mode).encode()}
                if model_skill.package_mode:
                    expected_files.update({name: content.encode() for name, content in model_skill.resources.items()})
                files = {p.relative_to(expected_mounted.parent).as_posix(): p.read_bytes()
                         for p in expected_mounted.parent.rglob('*') if p.is_file()}
                if files != expected_files or any(p.is_symlink() for p in expected_mounted.parent.rglob('*')):
                    self.error(slot/'skills', 'mounted_full_package_mismatch_or_extra_files')
                row['official_evidence_valid'] = True
                row['reward'] = reward if record.get('status') == 'valid' else None
            except Exception as exc:
                row['outcome_issues'].append('official_artifact_validation:'+type(exc).__name__)
                if record.get('status') == 'valid':
                    self.error(slot/'record.json', 'claimed_valid_trial_failed_official_artifact_validation')
            trace_path = result_path.parent/'agent/trajectory.json'
            trace = self.read_optional(trace_path)
            if trace:
                row['tokens'] = tokens(trace.get('final_metrics'))
                row['rounds'] = len(trace.get('steps', []))
                row['tool_calls'] = sum(len(s.get('tool_calls') or []) for s in trace.get('steps', []))
                if not (recovery and recovery.get('record_recovered') and record.get('metrics') is None):
                    self.same(slot/'record.json', record.get('metrics'), trace.get('final_metrics'), 'record_token_metrics_drift')
                references = []
                for index, step in enumerate(trace.get('steps', [])):
                    for call in step.get('tool_calls') or []:
                        arguments = json.dumps(call.get('arguments', {}), ensure_ascii=False)
                        for resource in skill.get('resources', {}) if skill.get('package_mode') else {}:
                            if resource in arguments:
                                references.append({'step_index': index, 'resource': resource,
                                    'tool': call.get('function_name'), 'command_arguments': call.get('arguments'),
                                    'observation_present': bool(step.get('observation')),
                                    'classification': 'resource_path_referenced_not_proof_of_execution'})
                row['resource_usage_evidence'] = references
                if not (recovery and recovery.get('record_recovered') and 'resource_usage_evidence' not in record):
                    self.same(slot/'record.json', record.get('resource_usage_evidence', []), references, 'resource_usage_evidence_drift')
                for label, filename in [('provider_request_records', 'raw_model_requests.jsonl'), ('provider_response_records', 'raw_model_responses.jsonl')]:
                    p = result_path.parent/'agent'/filename
                    row[label] = sum(1 for _ in p.open()) if p.exists() else None
                    if label in record:
                        self.same(slot/'record.json', record[label], row[label], label+'_drift')
                responses = result_path.parent/'agent/raw_model_responses.jsonl'
                models, statuses, request_attempts = Counter(), Counter(), Counter()
                if responses.exists():
                    for line in responses.open():
                        try:
                            response = json.loads(line)
                            if response.get('model'):
                                models[str(response['model'])] += 1
                            statuses[str(response.get('http_status'))] += 1
                            request_attempts[str(response.get('request_id'))] += 1
                        except ValueError:
                            self.error(responses, 'invalid_provider_response_jsonl')
                row['response_models'] = dict(models)
                row['provider_http_status_counts'] = dict(statuses)
                row['provider_request_ids_with_multiple_attempts'] = sum(n > 1 for n in request_attempts.values())
            else:
                trace = {'steps': []}
        else:
            trace = {'steps': []}
            row['outcome_issues'].append('no_official_result')
            if record.get('status') == 'valid':
                self.error(slot/'record.json', 'claimed_valid_without_official_result')
        row['valid'] = record.get('status') == 'valid' and row.get('official_evidence_valid') is True and row['fresh_unique']
        row['valid'] = row['valid'] and not any(e['path'].startswith(row['path']+'/') for e in self.errors)
        if not row['valid']:
            row['reward'] = None
        if development:
            feedback = self.read_optional(slot/'feedback.json')
            if feedback is None:
                self.wait(slot/'feedback.json', 'required_development_feedback_missing')
            else:
                expected = {'status': record['status'], 'reward': record.get('reward') if record['status'] == 'valid' else None,
                            'tool_observations': self.native.filter.project_tool_results(trace),
                            'projection': 'fixed-tool-results-8192-deny-evaluation-v1'}
                self.same(slot/'feedback.json', feedback, expected, 'development_feedback_projection_or_fields_drift')
        elif (slot/'feedback.json').exists():
            self.error(slot/'feedback.json', 'baseline_or_final_feedback_is_forbidden')
        return row

    def generation(self, task, arm, number):
        ar = self.root/'tasks'/task/'arms'/arm
        folder = ar/f'candidate-{number:03d}'
        row = {'path': str(folder.relative_to(self.root)), 'task': task, 'arm': arm, 'number': number,
               'reserved': (folder/'reserved.json').exists(), 'finished': False, 'generated': (folder/'skill.json').exists(),
               'tokens': tokens(None), 'errors': []}
        self.generations.append(row)
        if not row['reserved']:
            self.wait(folder, 'required_generation_slot_not_reserved')
            return row
        status = self.read_optional(folder/'generation_status.json')
        if status is None:
            self.wait(folder, 'reserved_generation_slot_not_finished')
            return row
        row.update(finished=True, status=status.get('status'), seconds=status.get('seconds'), error_type=status.get('error_type'))
        recovery = self.recoveries.get(folder)
        if recovery:
            row['infrastructure_recovery'] = {'reason': recovery['reason'],
                'source_artifact_lost': recovery.get('source_artifact_lost', False),
                'generator_sse_complete': recovery.get('generator_sse_complete', False), 'no_retry': True}
            if recovery.get('generation_recovered') and not recovery.get('generator_sse_complete'):
                self.error(folder, 'recovered_generation_without_declared_complete_sse')
            if not recovery.get('generator_sse_complete') and (row['generated'] or (folder/'completion.json').exists() and (folder/'completion.json') not in self.recovered_paths):
                self.error(folder, 'incomplete_recovered_generation_claims_package_or_completion')
        if status.get('no_retry') is not True:
            self.error(folder/'generation_status.json', 'generation_no_retry_marker_missing')
        if list(folder.glob('generation_attempts/*')):
            self.error(folder, 'undeclared_generation_attempt_directory')
        frozen = self.root/'tasks'/task/'frozen'
        base = read(frozen/'seed.json')
        paths, feedback = [], []
        previous = ar/'candidate-001'
        if number == 2 and (previous/'skill.json').exists():
            base = read(previous/'skill.json')
            paths = [previous/'dev'/f'{i:02d}'/'feedback.json' for i in range(1, 4)]
            try:
                feedback = [read(p) for p in paths]
            except (ValueError, OSError):
                self.error(folder, 'candidate_two_missing_exact_same_arm_dev_feedback')
        evidence = read(frozen/f'evidence-{arm}.json')
        expected_input = {'base_skill': base, 'evidence': evidence, 'feedback': feedback,
                          'feedback_paths': [str(p.relative_to(self.root)) for p in paths]}
        actual_input = self.read_optional(folder/'generation_input.json')
        if actual_input is not None:
            self.same(folder/'generation_input.json', actual_input, expected_input, 'generation_input_or_feedback_isolation_drift')
        elif row['generated'] or (folder/'response.sse').exists():
            self.error(folder, 'model_output_without_generation_input')
        expected_request = self.native.request(base, evidence, feedback, self.settings)
        request = self.read_optional(folder/'request.json')
        if request is not None:
            self.same(folder/'request.json', request, expected_request, 'exact_native_generation_request_drift')
        elif row['generated'] or (folder/'response.sse').exists():
            self.error(folder, 'model_output_without_exact_request')
        completion = self.read_optional(folder/'completion.json')
        outcome = self.read_optional(folder/'outcome.json')
        metrics = self.read_optional(folder/'generation_metrics.json')
        row['tokens'] = tokens((metrics or {}).get('usage'))
        if completion is not None:
            try:
                sse = (folder/'response.sse').read_bytes()
                reconstructed = self.native.relay.coalesce_sse([sse])
                self.same(folder/'completion.json', completion, reconstructed, 'completion_does_not_match_native_sse')
                raw = completion['choices'][0]['message']['content']
                native_outcome = self.native.outcome(base, evidence, feedback, raw)
                if outcome is None:
                    if status.get('status') != 'failed':
                        self.error(folder, 'complete_model_output_without_native_outcome')
                else:
                    self.same(folder/'outcome.json', without_creation_time(outcome), without_creation_time(native_outcome), 'native_proposal_outcome_or_origin_drift')
                    if outcome.get('candidate'):
                        candidate = outcome['candidate']['skill']
                        self.same(folder/'skill.json', self.read_optional(folder/'skill.json'), candidate, 'saved_skill_differs_from_native_outcome')
                        if not isinstance(candidate.get('provenance', {}).get('created_at'), str):
                            self.error(folder/'outcome.json', 'native_provenance_creation_timestamp_missing')
                        if status.get('status') != 'generated':
                            self.error(folder/'generation_status.json', 'package_and_generation_status_disagree')
                    elif row['generated'] or status.get('status') != outcome.get('reason'):
                        self.error(folder, 'no_change_invalid_outcome_has_skill_or_wrong_status')
                usages = [e['usage'] for e in self.native.relay.iter_sse_events([sse]) if e.get('usage')]
                expected_usage = usages[-1] if usages else completion.get('usage')
                self.same(folder/'generation_metrics.json', (metrics or {}).get('usage'), expected_usage, 'generation_token_usage_drift')
                self.same(folder/'generation_metrics.json', (metrics or {}).get('response_model'), completion.get('model'), 'generation_response_model_drift')
            except Exception as exc:
                self.error(folder, 'native_generation_replay_failed:'+type(exc).__name__)
        elif outcome is not None or row['generated'] or status.get('status') not in ('failed', 'interrupted_unresolved'):
            self.error(folder, 'generation_outcome_without_completion')
        elif recovery and not recovery.get('generator_sse_complete'):
            self.source_limit(folder/'response.sse', 'generator_stream_incomplete_no_completion_or_package_credit')
        row['errors'] = [e['code'] for e in self.errors if e['path'].startswith(row['path']+'/') or e['path'] == row['path']]
        return row

    def audit_arm(self, task, arm):
        ar = self.root/'tasks'/task/'arms'/arm
        candidates = []
        for number in (1, 2):
            generation = self.generation(task, arm, number)
            folder = ar/f'candidate-{number:03d}'
            generated = generation['generated']
            records = []
            if generated:
                skill = read(folder/'skill.json')
                records = [self.trial(folder/'dev'/f'{i:02d}', task, skill, True) for i in range(1, 4)]
            else:
                for i in range(1, 4):
                    if (folder/'dev'/f'{i:02d}'/'reserved.json').exists():
                        self.error(folder/'dev', 'development_for_no_usable_package')
            candidate = {'number': number, 'generated': generated,
                         'successes': sum(r['valid'] and r['reward'] == 1 for r in records),
                         'valid': sum(r['valid'] for r in records),
                         'rewards': [r['reward'] if r['valid'] else None for r in records]}
            candidates.append(candidate)
            if generation['finished']:
                summary = self.read_optional(folder/'development_summary.json')
                if summary is None:
                    self.wait(folder, 'development_summary_missing')
                elif not generated or all(r['finished'] for r in records):
                    self.same(folder/'development_summary.json', summary, candidate, 'development_summary_disagrees_with_audited_results')
        usable = [c for c in candidates if c['generated']]
        selected = min(usable, key=lambda c: (-c['successes'], c['number']))['number'] if usable else None
        expected_selection = {'rule': 'max valid dev successes, tie earliest; no usable package skips final',
                              'candidates': candidates, 'selected': selected}
        selection = self.read_optional(ar/'selection.json')
        if selection is None:
            self.wait(ar/'selection.json', 'selection_not_frozen')
            return {'task': task, 'arm': arm, 'candidates': candidates, 'selected': selected, 'finished': False}
        self.same(ar/'selection.json', selection, expected_selection, 'selection_differs_from_fixed_audited_rule')
        finals = []
        if selected is not None:
            source = ar/f'candidate-{selected:03d}'/'skill.json'
            frozen_final = ar/'final/skill.json'
            try:
                if source.read_bytes() != frozen_final.read_bytes():
                    self.error(frozen_final, 'final_skill_not_exact_selected_candidate_bytes')
                skill = read(frozen_final)
                finals = [self.trial(ar/'final'/f'{i:02d}', task, skill) for i in range(1, 6)]
            except (ValueError, OSError):
                self.wait(frozen_final, 'selected_final_skill_missing_or_invalid')
            expected_summary = {'status': 'complete', 'attempts': 5, 'selected': selected,
                                'rewards': [r['reward'] if r['valid'] else None for r in finals],
                                'successes': sum(r['valid'] and r['reward'] == 1 for r in finals),
                                'valid': sum(r['valid'] for r in finals)}
        else:
            expected_summary = {'status': 'no_usable_candidate', 'attempts': 0}
            for i in range(1, 6):
                if (ar/'final'/f'{i:02d}'/'reserved.json').exists():
                    self.error(ar/'final', 'final_trials_without_usable_generated_candidate')
        summary = self.read_optional(ar/'final_summary.json')
        if summary is None:
            self.wait(ar, 'final_summary_missing')
        elif selected is None or len(finals) == 5 and all(r['finished'] for r in finals):
            self.same(ar/'final_summary.json', summary, expected_summary, 'final_summary_disagrees_with_audited_results')
        return {'task': task, 'arm': arm, 'candidates': candidates, 'selected': selected,
                'final': expected_summary, 'finished': summary is not None and (selected is None or len(finals) == 5 and all(r['finished'] for r in finals))}

    def build(self):
        binding_count = self.check_freeze()
        self.check_recoveries()
        inventory = self.inventory()
        self.snapshot_inventory()
        baseline = []
        for task in TASKS:
            tr = self.root/'tasks'/task
            skill = read(tr/'frozen/seed.json')
            rows = [self.trial(tr/'baseline'/f'{i:02d}', task, skill) for i in range(1, 6)]
            summary = {'rewards': [r['reward'] if r['valid'] else None for r in rows],
                       'valid': sum(r['valid'] for r in rows),
                       'successes': sum(r['valid'] and r['reward'] == 1 for r in rows)}
            actual = self.read_optional(tr/'baseline/summary.json')
            if actual is None:
                self.wait(tr/'baseline', 'baseline_summary_missing')
            elif all(r['finished'] for r in rows):
                self.same(tr/'baseline/summary.json', actual, summary, 'baseline_summary_disagrees_with_audited_results')
            baseline.append({'task': task, **summary})
            for arm in ARMS:
                self.arms.append(self.audit_arm(task, arm))
        ledger = self.read_optional(self.root/'identity_ledger.json') or {}
        for identity, entries in self.identities.items():
            if identity not in self.excluded and len(entries) == 1:
                slot = self.root/entries[0]['slot']
                recovered = self.recoveries.get(slot, {})
                if (slot/'record.json').exists() and ledger.get(identity) != entries[0]['slot'] and not recovered.get('record_recovered'):
                    self.error(self.root/'identity_ledger.json', 'identity_ledger_missing_or_mismatched_slot')
        for identity, slot in ledger.items():
            if not any(e['slot'] == slot for e in self.identities.get(identity, [])):
                recovered = self.recoveries.get(self.root/slot, {})
                if recovered and recovered.get('source_artifact_lost'):
                    self.source_limit(self.root/slot, 'identity_ledger_unverifiable_after_snapshot_loss_no_identity_credit')
                else:
                    self.error(self.root/'identity_ledger.json', 'identity_ledger_without_authoritative_snapshot')
        trials = list(self.trials.values())
        complete = not self.pending and all(r['finished'] for r in trials) and all(g['finished'] for g in self.generations)
        protocol_passed = not self.errors
        return {'format': 'three-task-completion-audit-v1', 'audited_at': datetime.now(timezone.utc).isoformat(),
                'root': str(self.root), 'remote_root_mapping': str(self.remote),
                'passed': complete and protocol_passed, 'protocol_complete': complete, 'protocol_passed': protocol_passed,
                'errors': self.errors, 'pending': self.pending, 'binding_files_checked': binding_count,
                'infrastructure_recovery_slots': [str(p.relative_to(self.root)) for p in self.recoveries],
                'source_artifact_limitations': self.source_limits,
                'all_required_source_artifacts_retained': not self.source_limits,
                'reservation_audit': inventory, 'historical_ids_count': len(self.excluded),
                'authoritative_new_ids': dict(self.identities), 'baseline': baseline, 'arms': self.arms,
                'generation_slots': self.generations, 'executor_slots': trials,
                'generation_tokens': aggregate_tokens(self.generations), 'executor_tokens': aggregate_tokens(trials),
                'valid_trials': sum(r['valid'] for r in trials),
                'successful_trials': sum(r['valid'] and r['reward'] == 1 for r in trials),
                'unresolved_finished_trials': sum(r['finished'] and not r['valid'] for r in trials),
                'native_replay': 'unchanged frozen prompt/propose_package AST nodes; recorded completion only; creation timestamp excluded from replay equality',
                'verifier_policy': 'frozen official validator reads outcome markers internally; stdout or assertion contents never emitted',
                'cost_policy': 'observed tokens only; unknown counters retained; no monetary cost inferred',
                'model_calls': 0, 'evaluator_executions': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--remote-root', default=DEFAULT_REMOTE_ROOT)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    output = args.output or args.root/'completion_audit.json'
    try:
        result = CompletionAudit(args.root, args.remote_root).build()
    except Exception as exc:
        # Do not echo exception text: validators may contain restricted stdout.
        result = {'format': 'three-task-completion-audit-v1', 'passed': False, 'protocol_complete': False,
                  'protocol_passed': False, 'errors': [{'path': str(args.root), 'code': 'audit_setup_or_artifact_failure:'+type(exc).__name__}],
                  'model_calls': 0, 'evaluator_executions': 0}
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix+'.tmp')
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n'); temp.replace(output)
    print(json.dumps({'passed': result['passed'], 'protocol_complete': result['protocol_complete'],
                      'errors': len(result['errors']), 'output': str(output)}))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
