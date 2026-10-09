"""Offline checks of the TerminalBench execution path.

TerminalBench rollouts are owned by the external Harbor/Tencent runner; these
tests stub that runner and check the worker conversions, the benchmark's own
audit, and that nothing falls through to a native environment.
"""
import json
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
import unittest.mock
from unittest.mock import patch

from omegaconf import OmegaConf

from skillexpand import schema as S
from skillexpand.benchmarks.terminalbench import audit_harbor_experience
from skillexpand.evaluation import workers as EW
from skillexpand.l1 import workers as LW
from skillexpand.runtime import parallel as PL


def harbor_cfg(tmp, mode='harbor_rollout'):
    return OmegaConf.create({
        'benchmark': {'name': 'terminalbench', 'task_file': str(tmp / 'tasks.json'),
                      'rollout': {'mode': mode,
                                  'runner_script': str(tmp / 'runner.sh')}}})


def experience(task_id=28, rewards=(True, False), skill_key='terminalbench.general@v0'):
    trials = tuple({'index': i + 1, 'phase': 'autonomous', 'status': 'completed',
                    'success': ok, 'termination': 'verifier', 'trajectory': f'/t{i}'}
                   for i, ok in enumerate(rewards))
    return S.TaskExperience(
        experience_id='discovery:0:28', benchmark='terminalbench', task_id=task_id,
        task='fix the failing test', family_id='general', split='train',
        reward=any(rewards), num_trials=len(trials), initial_skill_key=skill_key,
        trial_rewards=tuple(rewards), trial_phases=tuple(t['phase'] for t in trials),
        experience_card={'schema_version': 5, 'task': {'task_id': task_id}},
        l1_trials=trials)


class HarborAuditTests(unittest.TestCase):
    def test_a_valid_harbor_experience_passes(self):
        report = audit_harbor_experience(experience())
        self.assertEqual(report, {'task_id': 28, 'trials': 2, 'reward': True,
                                  'skill_key': 'terminalbench.general@v0',
                                  'audit': 'terminalbench-harbor-v1'})

    def test_reward_or_trial_mismatches_are_rejected(self):
        contiguous = experience(rewards=(False, True))
        trials = list(contiguous.l1_trials)
        trials[1] = dict(trials[1], index=5)
        with self.assertRaisesRegex(ValueError, 'not contiguous'):
            audit_harbor_experience(replace(contiguous, l1_trials=tuple(trials)))
        wrong = experience(rewards=(True, True))
        wrong = replace(wrong, trial_rewards=(False, True))
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            audit_harbor_experience(wrong)
        wrong = replace(experience(rewards=(True,)), reward=False)
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            audit_harbor_experience(wrong)
        no_card = replace(experience(), experience_card={'schema_version': 4})
        with self.assertRaisesRegex(ValueError, 'schema-5'):
            audit_harbor_experience(no_card)


class HarborRolloutTests(unittest.TestCase):
    def test_the_executor_model_comes_from_the_frozen_config(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'tasks.json').write_text(json.dumps(
            [{'task_name': 'tb-task', 'instruction': 'fix the failing test'}]))
        (tmp / 'runner.sh').write_text('')
        cfg = harbor_cfg(tmp)
        cfg.agent = {'llm': 'openai/frozen-executor'}
        from skillexpand.benchmarks import terminalbench as TB
        completed = unittest.mock.Mock(returncode=0, stdout='', stderr='')
        with patch.dict('os.environ', {'MODEL_NAME': 'stale-shell-model'}), patch.object(
                TB.subprocess, 'run', return_value=completed) as run:
            TB.harbor_rollout(cfg, 0, None, 1, tmp / 'out')
        # The caller's environment never decides the model; the runner adds
        # the provider prefix itself.
        self.assertEqual(run.call_args.kwargs['env']['MODEL_NAME'], 'frozen-executor')


class HarborL1WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / 'tasks.json').write_text(json.dumps(
            [{'task_name': 'tb-task', 'instruction': 'fix the failing test'}]))
        self.cfg = harbor_cfg(self.tmp)

    def rollout(self, reward=True):
        return ({'task_name': 'tb-task', 'attempts': 1, 'trials': [
            {'attempt_index': 1, 'reward': int(reward), 'status': 'completed',
             'trajectory_path': str(self.tmp / 'trajectory.json'),
             'trial_dir': str(self.tmp), 'result_path': str(self.tmp / 'result.json')}]},
                {'run_id': 'run-1', 'out_dir': str(self.tmp), 'returncode': 0})

    def spec(self):
        return LW.ExperienceSpec(
            unit_id='discovery:28', benchmark='terminalbench', task_id=0,
            family_id='general', split='train', skill_aware=False,
            selection_source=S.SELECTION_UNSKILLED, max_trials=1,
            l1_checkpoint_path=str(self.tmp / 'evolution' / 'round-1' / 'trials' / '0.json'),
            evolution_round=1)

    def test_the_worker_builds_a_card_from_the_saved_rollout(self):
        (self.tmp / 'trajectory.json').write_text(json.dumps(
            {'steps': [{'message': 'run pytest', 'observation': '1 failed'}]}))
        with patch.object(PL, '_config', return_value=self.cfg), patch.object(
                LW.F, 'task_table', return_value=[{'task': 'fix the failing test'}]), patch(
                'skillexpand.benchmarks.terminalbench.harbor_rollout',
                return_value=self.rollout(reward=True)) as harbor:
            record = LW.execute_experience(self.spec())
        self.assertTrue(record['ok'])
        self.assertIsNone(record['failure'])
        self.assertEqual(harbor.call_args[0][3], 1)  # attempts: the spec budget
        exp = S.from_dict(S.TaskExperience, record['experience'])
        self.assertEqual(exp.reward, True)
        self.assertEqual(exp.trial_rewards, (True,))
        self.assertEqual(exp.experience_card['schema_version'], 5)
        self.assertEqual(exp.experience_card['task']['task_id'], 0)
        self.assertEqual(exp.experience_card['evidence'][0]['action'], 'TerminalBatch')
        audit_harbor_experience(exp)

    def test_a_failed_verifier_is_recorded_not_raised(self):
        with patch.object(PL, '_config', return_value=self.cfg), patch.object(
                LW.F, 'task_table', return_value=[{'task': 'fix the failing test'}]), patch(
                'skillexpand.benchmarks.terminalbench.harbor_rollout',
                return_value=self.rollout(reward=False)):
            record = LW.execute_experience(self.spec())
        self.assertTrue(record['ok'])
        exp = S.from_dict(S.TaskExperience, record['experience'])
        self.assertEqual((exp.reward, exp.trial_rewards), (False, (False,)))

    def test_other_benchmarks_never_reach_harbor(self):
        spec = LW.ExperienceSpec(
            unit_id='discovery:0', benchmark='searchqa', task_id=0,
            family_id='f', split='train', skill_aware=False, max_trials=1)
        with patch('skillexpand.benchmarks.terminalbench.harbor_rollout',
                   side_effect=AssertionError('harbor reached')) as harbor, patch.object(
                PL, '_config', return_value=OmegaConf.create(
                    {'benchmark': {'name': 'searchqa'}})), patch(
                    'skillexpand.l1.experience.gather_task_experience',
                    side_effect=RuntimeError('native path')):
            record = LW.execute_experience(spec)
        # A failed unit is reported, never raised; the native path ran.
        self.assertFalse(record['ok'])
        self.assertIn('native path', record['failure']['message'])
        self.assertEqual(harbor.call_count, 0)


class HarborFixedWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / 'tasks.json').write_text(json.dumps(
            [{'task_name': 'tb-task', 'instruction': 'fix the failing test'}]))
        self.cfg = harbor_cfg(self.tmp)

    def fixed_spec(self):
        return EW.FixedSpec('unit', 'terminalbench', 0,
                            skill_key='terminalbench.general@v1', skill_body='Be careful.')

    def rollout(self, reward):
        return ({'task_name': 'tb-task', 'attempts': 1, 'trials': [
            {'attempt_index': 1, 'reward': int(reward), 'status': 'completed',
             'trajectory_path': str(self.tmp / 'trajectory.json'),
             'exception_type': None if reward else 'AgentTimeout'}]},
                {'run_id': 'run-2', 'out_dir': str(self.tmp), 'returncode': 0})

    def test_one_attempt_and_the_verifier_result_decide_success(self):
        (self.tmp / 'trajectory.json').write_text(json.dumps(
            {'steps': [{'message': 'run pytest', 'observation': '1 passed'}]}))
        with patch.object(PL, '_config', return_value=self.cfg), patch(
                'skillexpand.benchmarks.terminalbench.harbor_rollout',
                return_value=self.rollout(True)) as harbor:
            record = EW.execute_fixed(self.fixed_spec())
        self.assertEqual(harbor.call_args[0][2].body, 'Be careful.')
        self.assertEqual(harbor.call_args[0][3], 1)  # single attempt, no reflection
        self.assertEqual(record['success'], True)
        self.assertIsNone(record['failure_mode'])
        self.assertEqual(record['steps'], 1)
        self.assertEqual(record['events'][0]['model_text'], 'run pytest')
        self.assertEqual(record['harbor_run_id'], 'run-2')

    def test_a_rejected_task_carries_the_verifier_failure(self):
        with patch.object(PL, '_config', return_value=self.cfg), patch(
                'skillexpand.benchmarks.terminalbench.harbor_rollout',
                return_value=self.rollout(False)):
            record = EW.execute_fixed(self.fixed_spec())
        self.assertEqual(record['success'], False)
        self.assertEqual(record['failure_mode'], 'AgentTimeout')
        self.assertEqual(record['events'], [])


if __name__ == '__main__':
    unittest.main()
