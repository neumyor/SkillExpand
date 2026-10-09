# TB2.1 Code Map

These experiments use all 89 tasks for train/evolution and closed-set acceptance.
Predicted acceptance scores are not verifier pass rates or independent test
generalization evidence. Runtime artifacts and credentials are excluded from Git.

## Current Entry Points

| Purpose | Script | Definition |
| --- | --- | --- |
| Fresh E1-aligned E3/E5 preparation, free library generation, routing, execution and evolution | `scripts/run_tb21_aligned.py` | [E3/E5 settings](TB21_E3_E5_ALIGNED_20261009.md) |
| Generate a library freely from experience cards | `scripts/propose_terminalbench_library.py` | Same E1 proposal schema; no bootstrap fallback |
| Materialize the generated v0 library | `scripts/materialize_terminalbench_library.py` | Model determines Skill count |
| Frozen-library closed-set execution | `scripts/run_tb21_empirical.py` | [E1 empirical evaluation](TB21_E1_EMPIRICAL_20261008.md) |
| Verifier coverage, pass@1/pass@3 and empirical report | `scripts/summarize_tb21_empirical.py` | Complete valid coverage required |
| Raw rollout coverage and exception classification | `scripts/validate_tb21_rollouts.py` | Raw timeout acceptance preserves original rewards |
| Completed batch audit | `scripts/audit_tb21_completed_batches.py` | Journal, gate and library consistency |

E4 waits for an audited evolved Skill from the new E3 experiment. The cancelled
bootstrap E4 run must not be resumed; see [E4 history](TB21_E4_EMPIRICAL_20261008.md).
[E6](TB21_E6_SETTING_20261009.md) is defined only and has not been launched.

## Historical Recovery And Diagnostics

`continue_tb21_authorized.py`, `restart_tb21_without_wire_format.py`,
`retry_e3_provider_default_output.py` and `reconcile_tb21_raw_gate_checkpoint.py`
retain the earlier experiments' recovery and authorization evidence. They do
not replace the new aligned entry point. Old bootstrap libraries, routes,
candidates, prediction caches and journals must not enter new E3/E5 runs.

`diagnose_empty_reviewer.py` and `diagnose_sse_transport.py` collect diagnostic
evidence; their results are not production gate results. See the
[503 diagnosis](TB21_503_DIAGNOSIS_20261008.md),
[empty-response diagnosis](TB21_EMPTY_REVIEWER_DIAGNOSIS_20261008.md) and
[DeepSeek output-budget diagnosis](TB21_E3_EMPTY_RESPONSE_20261009.md).

## Runtime And Checks

`src/skillexpand/runtime/llm_relay.py` owns Tencent relay transport and SSE
validation. `src/skillexpand/runtime/models/llm.py` applies model request policy.
DeepSeek defaults to `max_tokens=65536`; explicit limits take precedence.
Thinking and streaming remain enabled for aligned DeepSeek requests.

All stages share the 100-worker ceiling and use task sandbox persistence=0.
See [concurrency settings](TB21_CONCURRENCY_20261009.md). Keep credentials in
process environment and run artifacts under ignored `runs/` directories.

Focused regression checks:

```bash
PYTHONPATH=src python -m pytest tests/test_tb21_aligned.py \
  tests/test_tb21_empirical.py tests/test_tb21_raw_gate.py \
  tests/test_llm_relay.py tests/test_runtime_failures.py -q
```
