import importlib.util
from pathlib import Path
import unittest

source = Path(__file__).resolve().parents[1] / 'scripts/launch_tb21_e10.py'
spec = importlib.util.spec_from_file_location('e10', source)
e10 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e10)


class E10ConfigTest(unittest.TestCase):
    def test_only_context_and_execution_adaptation_change_source(self):
        source = {'job_name': '1001', 'jobs_dir': 'old', 'n_attempts': 3,
            'orchestrator': {'n_concurrent_trials': 100, 'retry': {'max_retries': 1}},
            'environment': {'import_path': 'original', 'kwargs': {'snapshot_retries': 3}},
            'agents': [{'import_path': 'original', 'model_name': 'openai/qwen3.6-flash-distill',
                'override_timeout_sec': 7200, 'kwargs': {'llm_call_kwargs': {'top_p': .95,
                    'max_tokens': 65536, 'extra_body': {'top_k': 20, 'enable_thinking': True}}}}]}
        current = e10.config(source)
        self.assertNotIn('model_info', source['agents'][0]['kwargs'])
        self.assertEqual(current['agents'][0]['kwargs']['model_info']['max_input_tokens'], 262144)
        self.assertEqual(current['agents'][0]['kwargs']['llm_call_kwargs'], source['agents'][0]['kwargs']['llm_call_kwargs'])
        self.assertEqual(current['orchestrator'], source['orchestrator'])
        self.assertEqual(current['n_attempts'], 3)
        self.assertNotIn('reasoning_effort', current['agents'][0]['kwargs']['llm_call_kwargs']['extra_body'])


if __name__ == '__main__':
    unittest.main()
