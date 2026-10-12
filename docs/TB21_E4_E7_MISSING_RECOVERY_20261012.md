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

## Recovery and source audit update

On 2026-10-12, E7 request 004 returned a real verifier reward of 0 and the
initial execution audit passed with 267 unique slots and 104 task-Skill cards.
E4 request 006 failed with an infrastructure timeout; request 007 is running.

The E7 library source audit found a material inconsistency. Although the
current proposal says 178 source cards and each Skill lists 178 provenance
IDs, the actual generation request 000 contains only 89 merged-source cards.
The current proposal bodies exactly equal the preserved legacy-89 proposal.
The proposal generator reused the cached legacy completion after the input
format change. No independent 178-card generation request is evidenced.

The 267 initial verifier scores remain valid for the actual executed library,
but cannot establish the requested independent 178-card library setting.
L2 must remain locked until this source inconsistency is resolved. Do not
rewrite Skill bodies or claim the existing scores evaluated a newly generated
library. No additional evaluation panel or budget is authorized by this audit.
