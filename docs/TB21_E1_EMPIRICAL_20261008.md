# E1 frozen final-library empirical evaluation

## Run

- Source: `tb21-e1-progressive-formal-20261006-repair9-stream-concurrent`.
- Remote root: `/data2/liyishan/SkillExpand-tb21/SkillExpand`.
- Independent run: `runs/tb21-e1-empirical-final-library-20261008`.
- Launch PID: 1740668.
- Frozen library: p001/p003 v0; p002/p004/p005/p006/p007 v1.
- Executor and selector: `qwen3.6-flash-distill`.
- Scope: 89 tasks, 3 independent attempts per task, closed-set empirical
  execution. No independent validation/test generalization claim.

## Execution and evidence

Each attempt independently performs metadata-only catalog selection, loads
exactly its selected frozen Skill, and invokes the existing Tencent E2B Harbor
adapter. The mounted Skill body and activation metadata are checked against the
frozen library. No native guard environment or LLM prediction is scored.

Canaries are attempt 1 of fix-git, log-summary-date-ranges, and
constraints-scheduling. Once all three produce valid verifier results (reward
0 or 1), those records count toward the full 267-attempt coverage. A task failure
is a valid canary result; a missing result or infrastructure error is not.

Full execution uses 16 workers. At launch E5 reserved 8 additional reviewer
workers, so total reserved concurrency was 24, below the 100-worker ceiling.
E3/E5 runs are not modified. Task sandbox persistence is 0. Harbor preserves
the baseline's 7200-second agent limit and executor sampling/output settings.

Slots are keyed by task_id and attempt_index. Up to three infrastructure
requests per unresolved slot are made per launch. Valid results are reused on
resume; failed requests and artifacts are retained separately. Timeouts, HTTP,
sandbox/runtime/verifier/persistence errors are never reward zero. Full scores
and complete status require all 267 valid unique slots and an artifact audit.

## Baseline limitation

The original bare rollout contains 267 trial identities, but 46 have
AgentTimeoutError/RuntimeError or other exception evidence. Only 221 are valid
under the current strict result policy. They remain excluded rather than being
converted into failures. Consequently the original baseline is incomplete;
no complete baseline pass@1/pass@3 or improvement delta is claimed. This does
not prevent reporting the new frozen-library scores if its coverage completes.

## Commands and outputs

`scripts/run_tb21_empirical.py --source <source> --run-dir <run> --workers 16`
prepares or resumes the run, performs canaries, and then fills the full panel.
`--prepare-only` validates source artifacts and writes independent frozen inputs
without issuing LLM or sandbox requests.

`scripts/summarize_tb21_empirical.py --run-dir <run>` regenerates result.json,
audit.json and report.md from verifier-backed slot records. Reported pass@1 is
mean success over all 267 attempts. pass@3 is the fraction of 89 tasks with at
least one success in their three attempts. Infrastructure failures are listed
separately. Baseline deltas require complete coverage and matching execution
settings; otherwise results are reported separately.

Focused scoring tests: 9 passed, including duplicate/incomplete slots, timeout
exclusion, verifier reward validation, task identity and mounted-body auditing.
All three canaries completed with reward 1 and passed the artifact audit. They
count as 3/267 valid attempts. Full-panel work has started with 16 workers;
final pass@1/pass@3 remains pending.

## 2026-10-08 18:40 CST checkpoint

The full-panel process remains alive, with 16 valid slots at inspection.
Task 0 / attempt 1 recorded a Harbor RuntimeError: tmux `send-keys` rejected
`--` as an invalid flag. The error remains outside the score denominator;
the existing worker performs bounded infrastructure retries without repeating
valid slots. This is a runtime adapter failure, not a verifier reward of zero.
No final metric or baseline improvement is reported.
