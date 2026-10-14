import copy
import importlib.util
import json
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location("input_filter", Path(__file__).parents[1] / "input_filter.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def tool_step(contents, arguments=None):
    return {
        "step_id": 9,
        "message": "Complete implementation and a passing self-test are described here.",
        "reasoning_content": "private narration",
        "tool_calls": [{"function_name": "shell", "arguments": arguments or {"command": "inspect"}}],
        "observation": {"results": [{"content": content} for content in contents]},
    }


class InputFilterTests(unittest.TestCase):
    def raw(self):
        return {
            "public_contract": "Produce the requested artifact.",
            "memory": {"mixed": {"experience_card": {"execution": {"success": True}, "code": "answer source"}}},
            "evidence": [
                {"id": str(index), "reward": reward, "skill_body": "historical solution clue",
                 "steps": [tool_step(["Unrecognized source syntax: BEGIN EXECUTABLE ANSWER"])]}
                for index, reward in enumerate([0, 0.0, False, True, 1, None, "0"])
            ],
        }

    def test_b_numeric_failure_whitelist_removes_all_free_text(self):
        raw = self.raw()
        original = copy.deepcopy(raw)
        result = module.project_history(raw, "B")
        self.assertEqual([row["id"] for row in result["historical_evidence"]], ["0", "1"])
        self.assertEqual(result["frozen_memory"], {})
        for row in result["historical_evidence"]:
            self.assertIsNone(row["loaded_skill"])
            self.assertEqual(row["steps"][0]["results"], [])
            self.assertEqual(row["steps"][0]["tool_functions"], ["shell"])
            self.assertEqual(row["steps"][0]["results_count"], 1)
        serialized = json.dumps(result)
        for secret in ["answer source", "historical solution clue", "EXECUTABLE ANSWER", "private narration"]:
            self.assertNotIn(secret, serialized)
        self.assertEqual(raw, original)

    def test_a_preserves_original_rows_card_and_allowed_echo(self):
        raw = self.raw()
        result = module.project_history(raw, "A")
        self.assertEqual(len(result["historical_evidence"]), 7)
        self.assertEqual(result["frozen_memory"]["mixed"], raw["memory"]["mixed"]["experience_card"])
        self.assertEqual(result["historical_evidence"][0]["loaded_skill"], "historical solution clue")
        self.assertIn("EXECUTABLE ANSWER", result["historical_evidence"][0]["steps"][0]["results"][0]["content"])
        result["frozen_memory"]["mixed"]["code"] = "changed"
        self.assertEqual(raw["memory"]["mixed"]["experience_card"]["code"], "answer source")

    def test_deny_checks_untruncated_center_and_removes_siblings(self):
        contents = ["a" * 5000 + "VeRiFiEr" + "b" * 5000, "otherwise allowed"]
        for structural in [False, True]:
            step = module.project_tool_results({"steps": [tool_step(contents)]}, structural)[0]
            self.assertEqual(step["results"], [])
            self.assertEqual(step["results_count"], 2)
            self.assertEqual(step["content_status"], "restricted_evaluation_output_removed")

    def test_nested_command_arguments_deny_all_result_content(self):
        for restricted in ["/TESTS/example", "Verifier", "REWARD.TXT", "test-stdout"]:
            trace = {"steps": [tool_step(["answer"], {"commands": [{"keystrokes": restricted}]})]}
            self.assertEqual(module.project_tool_results(trace)[0]["results"], [])

    def test_head_tail_limit_and_development_content_default(self):
        content = "h" * 4096 + "center" * 100 + "t" * 4096
        step = module.project_tool_results({"steps": [tool_step([content])]})[0]
        result = step["results"][0]
        self.assertEqual(result["content"], "h" * 4096 + "t" * 4096)
        self.assertEqual(result["original_characters"], len(content))
        self.assertTrue(result["truncated"])
        short = module.bounded_content("x" * 8192)
        self.assertFalse(short["truncated"])
        self.assertEqual(len(short["content"]), 8192)

    def test_no_tool_narration_is_not_projected_and_no_observation_is_safe(self):
        trace = {"steps": [{"message": "solution"}, {"step_id": 2, "tool_calls": [{"function_name": "shell"}]}]}
        steps = module.project_tool_results(trace)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["step_index"], 1)
        self.assertFalse(steps[0]["observation_present"])
        self.assertEqual(steps[0]["results_count"], 0)

    def test_unknown_arm_rejected(self):
        with self.assertRaises(ValueError):
            module.project_history(self.raw(), "C")

    def test_seed_wholesale_cleanup_is_identical_for_different_content(self):
        identity = {"skill_id": "generic", "family_id": "family", "version": 1,
                    "name": "Artifact work", "description": "Produce formatted artifacts."}
        for body in ["old source implementation", "mixed successful and failed rules"]:
            raw = {**identity, "body": body, "resources": {"solver.py": "answer"},
                   "provenance": {"source_experience_ids": ["successful-source"]},
                   "extra": "must not survive", "package_mode": True}
            original = copy.deepcopy(raw)
            result = module.sanitize_seed(raw)
            self.assertEqual(result, {**identity, "body": "Follow the public task requirements.",
                                     "resources": {}, "provenance": {}, "package_mode": False})
            self.assertEqual(raw, original)
            self.assertNotIn("answer", json.dumps(result))
            self.assertNotIn("successful-source", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
