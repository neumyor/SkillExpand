import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omegaconf import OmegaConf
from skillexpand import schema as S
from skillexpand.runtime.parallel import ExperienceSpec, execute_experience


class FrozenSelectorRecoveryTests(unittest.TestCase):
    def run_cached(self, task_text='task', raw='SKILL: terminalbench.general\nWHY: inspection'):
        skill = S.Skill('terminalbench.general', 'general', 0, 'general', 'inspection', 'body')
        cfg = OmegaConf.create({'benchmark': {'name': 'terminalbench', 'rollout': {'mode': 'harbor_rollout'}}})
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'selector.json'
            source.write_text(json.dumps({'ok': True, 'experience': {'task_id': 28, 'task': task_text},
                'selection': {'ok': True, 'skill_id': skill.skill_id, 'raw': raw,
                              'catalog': [{'skill_id': skill.skill_id, 'description': skill.description}]}}))
            spec = ExperienceSpec('evolution:1:28', 'terminalbench', 28, 'unassigned', 'train',
                progressive_selection=True, skill_library=(S.to_dict(skill),),
                frozen_selector_result=str(source), l1_checkpoint_path=str(Path(directory) / 'trials/28.json'))
            with patch('skillexpand.runtime.parallel._config', return_value=cfg), \
                    patch('skillexpand.runtime.parallel.F.task_table', return_value=[{'task': 'task'}] * 29), \
                    patch('skillexpand.runtime.parallel.F.build_reasoning_host', side_effect=AssertionError('new selector request')), \
                    patch('skillexpand.benchmarks.terminalbench.harbor_rollout', side_effect=RuntimeError('harbor reached')) as harbor:
                result = execute_experience(spec)
                return result, harbor.call_count

    def test_exact_identity_reuses_selector_without_provider_request(self):
        result, calls = self.run_cached()
        self.assertEqual(calls, 1)
        self.assertIn('harbor reached', result['error'])

    def test_task_mismatch_never_runs_harbor(self):
        result, calls = self.run_cached(task_text='different task')
        self.assertEqual(calls, 0)
        self.assertIn('identity mismatch', result['error'])

    def test_unknown_skill_raw_never_runs_harbor(self):
        result, calls = self.run_cached(raw='SKILL: unknown\nWHY: inspection')
        self.assertEqual(calls, 0)
        self.assertFalse(result['ok'])


if __name__ == '__main__':
    unittest.main()
