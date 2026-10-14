"""Offline delivered-archive fixtures; no provider, sandbox, or evaluator runs."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from completion_audit import CompletionAudit, DEFAULT_REMOTE_ROOT, TASKS, ARMS, tokens


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def make_static_fixture(root):
    """Create an archive from repository code and invented inputs only.

    Source files are copied, rather than linked to an experiment archive, so
    exact-byte binding checks exercise separate current/snapshot artifacts.
    No historical trajectories, tasks, memories or measured results are read.
    """
    repository = Path(__file__).resolve().parents[2]
    native_files = (
        '__init__.py', 'schema.py', 'skill_package.py',
        'l1/__init__.py', 'l1/family_discovery.py', 'l2/editor.py',
        'runtime/llm_relay.py',
    )
    for name in native_files:
        target = root/'code/skillexpand'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repository/'src/skillexpand'/name, target)
    shutil.copyfile(repository/'scripts/summarize_tb21_empirical.py',
                    root/'code/summarize_tb21_empirical.py')
    shutil.copyfile(Path(__file__).with_name('input_filter.py'), root/'input_filter.py')
    put(root/'frozen/generation_settings.json', {
        'model': 'fixture-method-model', 'stream': True,
        'max_tokens': 65536, 'enable_thinking': True,
    })
    put(root/'frozen/historical_sandbox_ids.json', ['fixture-historical-id'])
    put(root/'manifest.json', {
        'tasks': list(TASKS), 'arms': list(ARMS), 'preaudit_passed': True,
        'budget': {'baseline_per_task': 5, 'candidates_per_arm': 2,
                   'dev_per_candidate': 3, 'final_per_arm': 5,
                   'max_executor_trials': 81, 'generation_slots': 12},
    })
    for task in TASKS:
        folder = root/'tasks'/task/'frozen'
        put(folder/'seed.json', {
            'skill_id': 'fixture-skill-'+task, 'family_id': 'fixture-family',
            'version': 0, 'name': 'Synthetic generic skill',
            'description': 'Follow the supplied public requirements.',
            'body': 'Follow the public task requirements.',
            'resources': {}, 'package_mode': False, 'provenance': {},
        })
        put(folder/'protocol.json', {'task_name': task,
                                    'model_name': 'qwen3.6-flash-distill'})
        put(folder/'executor.json', {
            'agent': {'model_name': 'openai/qwen3.6-flash-distill',
                      'import_path': 'fixture:TencentSkillTerminus2',
                      'override_timeout_sec': 7200,
                      'kwargs': {'llm_call_kwargs': {'top_p': 0.95}}},
            'verifier': {'override_timeout_sec': 60},
            'environment': {'kwargs': {'skill_root': 'fixture-unmounted'}},
            'task': {'path': 'fixture-task-path'},
        })
        for arm in ARMS:
            put(folder/f'evidence-{arm}.json', {
                'public_contract': 'Synthetic task; no benchmark implementation.',
                'frozen_memory': {}, 'evidence': [], 'fixture_arm': arm,
            })
    bindings = []
    sources = sorted(p for p in root.rglob('*') if p.is_file())
    for index, source in enumerate(sources):
        snapshot = root/'binding_snapshots'/f'{index:04d}.bin'
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_bytes(source.read_bytes())
        bindings.append({'current': str(source.relative_to(root)),
                         'snapshot': str(snapshot.relative_to(root))})
    put(root/'bindings.json', bindings)


class CompletionAuditTests(unittest.TestCase):
    def make_archive(self, root, packages=False):
        make_static_fixture(root)
        audit = CompletionAudit(root)
        ledger = {}
        def remote(path):
            return str(Path(DEFAULT_REMOTE_ROOT)/path.relative_to(root))
        def reservation(path, kind):
            put(path/'reserved.json', {'kind': kind, 'slot': str(path.relative_to(root))})
        def trial(path, task, skill, development=False):
            reservation(path, 'executor')
            frozen = root/'tasks'/task/'frozen'
            executor = json.loads((frozen/'executor.json').read_text())
            protocol = json.loads((frozen/'protocol.json').read_text())
            job = path/'jobs/job';official = job/(task+'__fixture')
            config = json.loads(json.dumps(executor))
            config['task']['path'] = remote(frozen/'tasks'/task)
            config['environment']['kwargs']['skill_root'] = remote(path/'skills')
            put(path/'input.json', {'skill': skill, 'executor': executor, 'task_path': remote(frozen/'tasks'/task), 'protocol': protocol})
            put(path/'started.json', {'at': 1})
            put(job/'config.json', {'n_attempts': 1, 'orchestrator': {'retry': {'max_retries': 0}, 'n_concurrent_trials': 1},
                                     'datasets': [{'path': remote(frozen/'tasks'), 'task_names': [task]}]})
            put(official/'result.json', {'task_name': task, 'config': config, 'exception_info': None,
                                         'verifier_result': {'rewards': {'reward': 1}}})
            identity = 'fixture-'+str(path.relative_to(root))
            put(official/'snapshot.json', {'source_sandbox_id': identity})
            ledger[identity] = str(path.relative_to(root))
            trace = {'steps': [], 'final_metrics': {'total_prompt_tokens': 7, 'total_completion_tokens': 3}}
            put(official/'agent/trajectory.json', trace)
            put(official/'agent/skill_activation.json', {'load_stage': 'after_selection',
                'source_path': remote(path/'skills'/task/'SKILL.md'), 'skill_path': '/opt/openclaw-skills/'+task+'/SKILL.md'})
            model = audit.native.schema.from_dict(audit.native.schema.Skill, skill)
            entry = path/'skills'/task/'SKILL.md';entry.parent.mkdir(parents=True)
            entry.write_text(audit.native.entrypoint_content(model, package=model.package_mode))
            if model.package_mode:
                for name, content in model.resources.items():
                    resource = entry.parent/name;resource.parent.mkdir(parents=True, exist_ok=True);resource.write_text(content)
            verifier = official/'verifier';verifier.mkdir()
            (verifier/'reward.txt').write_text('1')
            # Marker-only synthetic verifier evidence; no task test is executed.
            (verifier/'test-stdout.txt').write_text('test session starts\n1 passed\n')
            put(path/'record.json', {'status': 'valid', 'reward': 1, 'sandbox_id': identity, 'identity_valid': True,
                'no_retry': True, 'metrics': trace['final_metrics'], 'result_path': remote(official/'result.json'),
                'trajectory_path': remote(official/'agent/trajectory.json')})
            if development:
                put(path/'feedback.json', {'status': 'valid', 'reward': 1, 'tool_observations': [],
                    'projection': 'fixed-tool-results-8192-deny-evaluation-v1'})
        for task in TASKS:
            tr = root/'tasks'/task;seed = json.loads((tr/'frozen/seed.json').read_text())
            for i in range(1, 6):
                trial(tr/'baseline'/f'{i:02d}', task, seed)
            put(tr/'baseline/summary.json', {'rewards': [1]*5, 'valid': 5, 'successes': 5})
            for arm in ARMS:
                ar = tr/'arms'/arm
                candidates = []
                for number in (1, 2):
                    folder = ar/f'candidate-{number:03d}';reservation(folder, 'generation')
                    base = seed if number == 1 or not packages else json.loads((ar/'candidate-001/skill.json').read_text())
                    evidence = json.loads((tr/f'frozen/evidence-{arm}.json').read_text())
                    paths = [ar/'candidate-001/dev'/f'{i:02d}'/'feedback.json' for i in range(1,4)] if packages and number == 2 else []
                    feedback = [json.loads(p.read_text()) for p in paths]
                    request = audit.native.request(base, evidence, feedback, audit.settings)
                    raw = json.dumps({'body': 'Autonomous generic help '+str(number), 'resources': {'helpers/check.txt': 'generic note'},
                                      'rationale': 'Public evidence'} if packages else {'no_change': True, 'reason': 'No supported gap'})
                    sse_event = {'model': 'fixture-model', 'choices': [{'delta': {'role': 'assistant', 'content': raw}, 'finish_reason': 'stop'}],
                                 'usage': {'prompt_tokens': 9, 'completion_tokens': 2}}
                    sse = ('data: '+json.dumps(sse_event)+'\n\ndata: [DONE]\n\n').encode()
                    completion = audit.native.relay.coalesce_sse([sse])
                    outcome = audit.native.outcome(base, evidence, feedback, raw)
                    put(folder/'generation_input.json', {'base_skill': base, 'evidence': evidence, 'feedback': feedback,
                                                         'feedback_paths': [str(p.relative_to(root)) for p in paths]})
                    put(folder/'request.json', request);(folder/'response.sse').write_bytes(sse)
                    put(folder/'completion.json', completion);put(folder/'outcome.json', outcome)
                    put(folder/'generation_metrics.json', {'usage': sse_event['usage'], 'response_model': 'fixture-model'})
                    put(folder/'generation_status.json', {'status': 'generated' if packages else outcome['reason'], 'no_retry': True})
                    if packages:
                        skill = outcome['candidate']['skill'];put(folder/'skill.json', skill)
                        for i in range(1,4):
                            trial(folder/'dev'/f'{i:02d}', task, skill, True)
                    row = {'number': number, 'generated': packages, 'successes': 3 if packages else 0,
                           'valid': 3 if packages else 0, 'rewards': [1]*3 if packages else []}
                    candidates.append(row);put(folder/'development_summary.json', row)
                put(ar/'selection.json', {'rule': 'max valid dev successes, tie earliest; no usable package skips final',
                                         'candidates': candidates, 'selected': 1 if packages else None})
                if packages:
                    skill = json.loads((ar/'candidate-001/skill.json').read_text());put(ar/'final/skill.json', skill)
                    # Freeze uses exact bytes, not merely JSON equivalence.
                    (ar/'final/skill.json').write_bytes((ar/'candidate-001/skill.json').read_bytes())
                    for i in range(1,6):
                        trial(ar/'final'/f'{i:02d}', task, skill)
                    put(ar/'final_summary.json', {'status': 'complete', 'attempts': 5, 'selected': 1,
                                                 'rewards': [1]*5, 'successes': 5, 'valid': 5})
                else:
                    put(ar/'final_summary.json', {'status': 'no_usable_candidate', 'attempts': 0})
        put(root/'identity_ledger.json', ledger)
        return audit

    def test_complete_no_change_slots_retained_without_finals(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.make_archive(Path(temp)).build()
            self.assertTrue(result['passed'], result['errors'])
            self.assertEqual(result['reservation_audit']['executor_reserved'], 15)
            self.assertEqual(result['reservation_audit']['generation_reserved'], 12)

    def test_full_81_archive_native_replay_and_tie_earliest(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.make_archive(Path(temp), packages=True).build()
            self.assertTrue(result['passed'], result['errors'])
            self.assertEqual(result['reservation_audit']['executor_reserved'], 81)
            self.assertTrue(all(a['selected'] == 1 for a in result['arms']))
            self.assertEqual(result['executor_tokens']['total']['observed_sum'], 810)

    def test_duplicate_even_unresolved_and_extra_resource_are_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp);audit = self.make_archive(root, packages=True)
            one = root/'tasks'/TASKS[0]/'baseline/01';two = root/'tasks'/TASKS[1]/'baseline/01'
            first = next(one.glob('jobs/*/*/snapshot.json'));second = next(two.glob('jobs/*/*/snapshot.json'))
            put(second, json.loads(first.read_text()))
            r = json.loads((two/'record.json').read_text());r['status'] = 'unresolved';put(two/'record.json', r)
            (one/'skills'/TASKS[0]/'extra.txt').write_text('extra')
            result = audit.build();codes = {e['code'] for e in result['errors']}
            self.assertFalse(result['passed'])
            self.assertIn('sandbox_historical_or_globally_reused', codes)
            self.assertIn('mounted_full_package_mismatch_or_extra_files', codes)

    def test_other_arm_feedback_and_harbor_retries_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp);audit = self.make_archive(root, packages=True)
            folder = root/'tasks'/TASKS[0]/'arms/A/candidate-002'
            p = folder/'generation_input.json';value = json.loads(p.read_text())
            value['feedback_paths'][0] = value['feedback_paths'][0].replace('/arms/A/', '/arms/B/');put(p, value)
            p = next((root/'tasks'/TASKS[0]/'baseline/01').glob('jobs/*/config.json'))
            value = json.loads(p.read_text());value['orchestrator']['retry']['max_retries'] = 1;put(p, value)
            result = audit.build();codes = {e['code'] for e in result['errors']}
            self.assertIn('generation_input_or_feedback_isolation_drift', codes)
            self.assertIn('harbor_attempts_or_retry_policy_drift', codes)

    def test_native_request_sse_and_final_feedback_tampering_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp);audit = self.make_archive(root, packages=True)
            candidate = root/'tasks'/TASKS[0]/'arms/A/candidate-001'
            p = candidate/'request.json';value = json.loads(p.read_text());value['max_tokens'] = 9;put(p, value)
            p = candidate/'completion.json';value = json.loads(p.read_text());value['choices'][0]['message']['content'] += ' ';put(p, value)
            put(root/'tasks'/TASKS[0]/'arms/A/final/01/feedback.json', {'forbidden': 'final'})
            result = audit.build();codes = {e['code'] for e in result['errors']}
            self.assertIn('exact_native_generation_request_drift', codes)
            self.assertIn('completion_does_not_match_native_sse', codes)
            self.assertIn('baseline_or_final_feedback_is_forbidden', codes)

    def test_valid_agent_timeout_still_counts_official_reward(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp);audit = self.make_archive(root)
            path = next((root/'tasks'/TASKS[0]/'baseline/01').glob('jobs/*/*/result.json'))
            value = json.loads(path.read_text());value['exception_info'] = {'exception_type': 'AgentTimeoutError'};put(path, value)
            result = audit.build()
            self.assertTrue(result['passed'], result['errors'])
            self.assertEqual(result['valid_trials'], 15)

    def test_token_unknowns_and_real_counter_names(self):
        self.assertIsNone(tokens({})['values']['total'])
        value = tokens({'total_prompt_tokens': 11, 'total_completion_tokens': 7, 'total_cached_tokens': 3})
        self.assertEqual(value['values'], {'input': 11, 'output': 7, 'cached': 3, 'total': 18})
        self.assertEqual(value['source_keys']['input'], 'total_prompt_tokens')


if __name__ == '__main__':
    unittest.main()
