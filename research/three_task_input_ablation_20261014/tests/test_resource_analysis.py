import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("resource_analysis", Path(__file__).parents[1] / "resource_analysis.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


def step(command, content="output", call_id="c1"):
    return {"step_id": 4, "tool_calls": [{"tool_call_id": call_id, "function_name": "bash",
            "arguments": {"commands": [{"keystrokes": command}]}}],
            "observation": {"results": [{"content": content}]}}


class ResourceAnalysisTests(unittest.TestCase):
    def test_read_copy_alias_and_invocation_are_distinct(self):
        trace = {"steps": [step("cat /mounted/helpers/check.py"),
                           step("cp /mounted/helpers/check.py /tmp/local.py"),
                           step("python3 /tmp/local.py"), step("echo helpers/check.py")]}
        events = M.analyze_resource_use(trace, {"helpers/check.py": ""})["events"]
        self.assertEqual([e["classification"] for e in events], ["read", "copy", "invocation", "reference"])
        self.assertEqual(events[2]["resource_match"], "copy_alias_unverified")
        self.assertTrue(all(e["outcome"] == "unknown" for e in events))
        self.assertEqual(events[0]["result_linkage"], "single_call_same_step")

    def test_observed_error_does_not_establish_resource_specific_failure(self):
        events = M.analyze_resource_use({"steps": [step("node scripts/check.js", "Error: bad input")]},
                                       {"scripts/check.js": ""})["events"]
        self.assertTrue(events[0]["observed_error_signal"])
        self.assertEqual(events[0]["outcome"], "unknown")

    def test_multi_call_outputs_need_exact_link(self):
        s = step("bash check.sh")
        s["tool_calls"].append({"tool_call_id": "c2", "function_name": "bash", "arguments": {"cmd": "ls"}})
        event = M.analyze_resource_use({"steps": [s]}, {"check.sh": ""})["events"][0]
        self.assertFalse(event["observed_result_present"])
        self.assertEqual(event["result_linkage"], "unlinked_same_step")
        s["observation"]["results"][0]["tool_call_id"] = "c1"
        event = M.analyze_resource_use({"steps": [s]}, {"check.sh": ""})["events"][0]
        self.assertEqual(event["result_linkage"], "tool_call_id")

    def test_restricted_step_suppresses_all_commands_and_outputs(self):
        events = M.analyze_resource_use({"steps": [step("python check.py; cat /tests/example", "hidden")]},
                                       {"check.py": ""})["events"]
        self.assertEqual(events, [{"step_index": 0, "step_id": 4,
                                  "classification": "restricted_evaluation_output_removed", "outcome": "unknown"}])

    def test_python_import_is_resolution_candidate_not_proven_execution(self):
        event = M.analyze_resource_use({"steps": [step('python3 -c "from helpers import check"')]},
                                      {"helpers/check.py": "", "helpers.py": ""})["events"][0]
        self.assertEqual(event["resource"], "helpers.py")
        self.assertEqual(event["classification"], "import_invocation_candidate")
        self.assertEqual(event["outcome"], "unknown")

    def test_heredoc_payload_is_not_shell_execution(self):
        command = "cat > generated.py <<'PY'\npython check.py\nPY\ncat check.py\n"
        events = M.analyze_resource_use({"steps": [step(command)]}, {"check.py": ""})["events"]
        self.assertEqual([e["classification"] for e in events], ["read"])

    def test_unknown_argument_fields_not_treated_as_commands(self):
        self.assertEqual(M.commands({"analysis": "python check.py"}), [])

    def test_output_redirection_does_not_count_as_read(self):
        events = M.analyze_resource_use({"steps": [step("cat data.txt > check.py")]}, {"check.py": ""})["events"]
        self.assertEqual(events[0]["classification"], "write_reference")

    def test_failure_markers_exclude_restricted_outputs(self):
        report = M.observed_failure_steps({"steps": [step("python check.py", "SyntaxError: bad"),
                                                      step("cat /tests/result", "FAILED hidden")]})
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]["resource_causation"], "unknown")
        self.assertNotIn("hidden", json.dumps(report))

    def test_experiment_keeps_no_change_and_unresolved_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = root / "tasks" / "example" / "arms" / "A" / "candidate-001"
            candidate.mkdir(parents=True)
            (candidate / "generation_status.json").write_text(json.dumps({"status": "no_effective_change"}))
            trial = candidate / "dev" / "01"
            trial.mkdir(parents=True)
            (trial / "record.json").write_text(json.dumps({"status": "unresolved", "reward": 0}))
            report = M.analyze_experiment(root)
            self.assertEqual(report["generation_statuses"][0]["status"], "no_effective_change")
            self.assertIsNone(report["trials"][0]["reward"])
            self.assertFalse(report["trials"][0]["trace_available"])


if __name__ == "__main__":
    unittest.main()
