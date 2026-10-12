# E4 and E7 Missing Slot Recovery

User-authorized recovery targets only E4 `09-2` (circuit-fibsqrt) and E7
`67-2` (qemu-alpine-ssh). Both panels retain their 266 accepted scores.

E4 history contains five requests, including a preserved empty result and
E2B command timeouts without rewards. E7 history contains three requests with
tmux and sandbox errors without rewards. This launch permits at most three
additional requests per missing slot, continuing numbering at 006 and 004.
No history or correction budget is reset; no missing reward is replaced with zero.

Recovery drivers launched on 2026-10-12 (Asia/Shanghai): E4 PID 799239,
E7 PID 799240. Shared capacity reservation is 32/250. No prior driver or
Harbor process for these panels was found. Model credentials are inherited
from an authorized process and the user-authorized Desktop environment through
SSH stdin, verified independently, and never persisted. Existing model roles,
Skill versions, timeouts, persistence=0 and pipeline v6 cache are retained.

E4 uses the frozen-library execution path and refreshes its complete report
and audit on return. E7 uses only the initial-library slot path and runs the
initial execution audit on completion. E7 L2 remains gated pending validation
of the 178-source-card input contract. E5_FINAL remains stopped.

The launched processes are not evidence of accepted scores. Check each run's
`missing_slot_resume.json`, `status.json`, request results and execution audit
for the recovery outcome; credential verification failure creates no new slot
request and leaves the run in needs_attention.
