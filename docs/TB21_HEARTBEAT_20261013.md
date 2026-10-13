# TB2.1 heartbeat checkpoint

## 2026-10-13 11:16 Asia/Shanghai heartbeat

E7 targeted slot 77-3 request 004 ended with TimeoutException and no reward.
Request 005 now has an actual raw_model_requests.jsonl; driver 606643 remains
alive in skill_rollouts. Existing requests 001-004 are retained. This launch
still allows at most three new requests (004-006); no valid slot is repeated.
Strict accepted initial coverage remains 262/267, and L2 stays locked. DNS and
C4 fixture-cache gaps at 51-2 and 73-1/2/3 remain unresolved and are not retried.

E9 configure-git-webserver physical_networkfix_02 completed with real verifier
reward 1. The original first-attempt evidence and repair backup remain intact.
All 89 initial task states now exist. Refreshed initial_audit.json accepts
41 successes and 42 failures, retaining six missing outcomes: three exhausted
infrastructure tasks, two RuntimeError framework-zero records and one C4
fixture setup error. The old audit is preserved in its backup.

The resumed E9 supervisor and evaluator exited with code 1. Its evaluation
log explicitly reports the initial barrier blocked by circuit-fibsqrt,
gpt2-codegolf and schemelike-metacircular-eval. No Ralph, curation, frozen
memory, memory-enhanced test or vanilla evaluation has run. Another resume
cannot resolve the exhausted physical-attempt budgets under the frozen
contract, so no restart, budget increase or barrier bypass is performed.
Owned-child readiness and supervisor/evaluator exit were verified; under the
shared capacity lock E9 registration now has workers=0 and phase
blocked_initial_barrier. Current recursive active reservation is 16/250 (E7).

E8 remains finished and audited_incomplete at 254/267 valid scores, 13 missing.
Final panels retain their historical reports and supplementary unresolved
verifier audits; none is rerun. E5 stays stopped. Automation remains active,
and every conclusion remains limited to the closed task set.

## 2026-10-13 10:13 Asia/Shanghai heartbeat

Corrected the earlier E7 retry-budget interpretation: three new requests are
allowed per slot per launch, retaining all cumulative history. Three historical
requests do not permanently exhaust E7. Recovery now preserves rejected old
records before retrying, and supports a targeted initial slot list without
bypassing the complete initial/L2 audit barrier.

Both authorized Desktop role keys were verified through Tencent sandbox
requests (Qwen executor/selector and DeepSeek method); credentials were sent
over SSH stdin into process memory only. E7 resumed only slot 77-3 with driver
606643 and request 004 actually created. The first launcher failed before any
request because PYTHONPATH was missing; its launch metadata and log remain,
and the corrected launcher uses the repository source path. Strict accepted
coverage remains 262/267 pending new verifier evidence. Other unresolved slots
51-2 and 73-1/2/3 are not retried yet: mirror DNS failure and an unavailable
Hugging Face C4 fixture cache configuration need infrastructure work. L2 stays
locked; no old parent scores or completions are imported.

E9 resumed its original contract after owned-child readiness and shared capacity
checks. Supervisor 603772, remote driver 603806 and fresh evaluation 607827
were verified live; fresh preflight passed. The second
configure-git-webserver physical attempt now has child.json, driver.log and a
jobs directory. This is execution launch evidence, not task completion.
The repaired first physical attempt and its original
backup are retained. Three exhausted infrastructure tasks still block teaching;
no additional attempts or contract changes are made for those tasks.

E9 initial audit is now based on raw verifier results, identity, model,
trajectory and executed test outcomes, with all 89 expected tasks represented:
40 success, 42 failure, 7 missing. The seven include two RuntimeError framework
zeros, three exhausted infrastructure tasks, configure-git-webserver's missing
state, and reshard-c4-data's fixture setup error. The old initial audit is
backed up; raw states are unchanged. No Ralph, freeze, test or vanilla acceptance.

Shared active reservation at launch was 80/250 (E9 64 plus E7 16). E8 remains
audited_incomplete at 254/267; Final supplementary gaps remain unresolved and
those panels are not rerun. E5 stays user-stopped. All conclusions are closed-set.
Automation remains active.
Focused remote validation passed 13 tests covering aligned recovery and the
E9 raw-verifier audit, including framework-zero and absent-state handling.

## 2026-10-13 08:13 Asia/Shanghai heartbeat

E7 remains stopped at the initial-only barrier. The strict supplementary audit
now accepts 262 scores; four records with reported zero lack executed verifier
test outcomes and remain unresolved. Slot 77-3 has three retained requests,
no reward, and is excluded. No L2 or fourth request was started.

E9 remains stopped after the initial barrier with 88/89 state records and
three blocked infrastructure tasks. The manually terminated
configure-git-webserver verifier was reclassified using the approved evidence
repair: its original `result.json` was backed up, then marked
`infra=true`, `infra_retryable`, `reward=null`; no attempt or budget was added.
The normal E9 resume can consume only the remaining configured physical
attempt, but no active credential process is available in this heartbeat, so
resume was not launched. circuit-fibsqrt, schemelike-metacircular-eval and
gpt2-codegolf remain exhausted after two TimeoutException attempts.

E8 remains audited_incomplete at 254/267 strict scores and 13 unknowns. E5 is
untouched and stopped. All conclusions remain closed-set.

## 2026-10-13 07:16 Asia/Shanghai heartbeat

E7 initial-only driver stopped with `needs_attention` because strict valid
coverage is incomplete. Slot 77-3 (`schemelike-metacircular-eval`) has three
retained requests and no valid verifier outcome, so its per-slot retry budget
is exhausted. The four previously rejected no-outcome slots remain excluded;
L2 cannot start. No fourth request or old score import was performed.

E9 stopped with `needs_attention` after the initial barrier. It has 88/89 task
state records: 42 completed, 43 initial_failed and 3 blocked infrastructure
budget units (`circuit-fibsqrt`, `schemelike-metacircular-eval`,
`gpt2-codegolf`). Their missing outcomes are preserved in `initial_audit.json`;
the framework reported `TransportExhaustedError` after each task's configured
two physical attempts. Because the initial barrier is unresolved, Ralph,
teacher curation, frozen memory, memory-enhanced test and vanilla do not run.
No new driver, retry-preflight, budget reset or contract change is authorized
by the existing E9 contract.

E8 remains finished but `audited_incomplete` at 254/267 strict scores and 13
unknown outcomes. E5 remains user-stopped; completed Final panels are not
rerun. Automation continues to retain these unresolved acceptance states.

## 2026-10-13 06:16 Asia/Shanghai heartbeat

E7 reached 260 historical valid records, with six live Harbor children for
tasks 87/88 and one infrastructure-error record at 77-3. Strict verifier
review remains the acceptance gate; the four known no-outcome records are
excluded, so recorded coverage is not accepted coverage. Driver is alive and
L2 remains locked.

E9 still has 87 state records. gpt2-codegolf is running physical_networkfix_02
within the original two-attempt infrastructure budget. The configure-git-webserver
owned process termination did take effect: physical result exists with
exit_code=-15, reward=null and failure_kind=evidence_invalid. The framework
classified this as a nonretryable evidence error rather than a retryable
verifier stall. Its task lock is no longer held; the batch parallel loop waits
for gpt2-codegolf before raising the saved error. No restart is safe while that
child remains active. Subsequent recovery must preserve this result and the
stall diagnostic, explicitly record any compatible classification repair,
retain cumulative attempts, and use the original --resume contract.

E8 and supplementary Final-panel gaps are unchanged; no reruns or budget
resets were performed, and E5 remains stopped.

## 2026-10-13 05:16 Asia/Shanghai heartbeat

E7 has 251 historical valid records; refreshed strict verifier review accepts
247 unique scores and rejects four: mteb-retrieve 51-2 and reshard-c4-data
73-1/2/3 lack executed pytest outcomes. Supplementary audit is refreshed.
The original initial-only driver remains alive. L2 remains locked.

E9 remains at 87 initial task states, with its evaluator alive. gpt2-codegolf
continues producing raw model requests. configure-git-webserver stalled after
pytest collected one test; verifier stdout had been unchanged for over 6,700
seconds versus its 900-second verifier budget. Its owned rollout process was
verified against child.json and stopped using stop_owned_process after saving
verifier_stall_recovery.json in physical_networkfix_01. No reward is fabricated
and no budget is reset. At the immediate post-stop check, the evaluator remained
alive but had not yet written a new task state or next physical attempt; this
does not establish successful recovery. The two previously blocked budget
units still prevent the teaching barrier from passing.

E8 remains audited_incomplete with 13 unknown outcomes. Final-panel
supplementary audit gaps remain unresolved; no Final panel or E5 is restarted.

## 2026-10-13 04:16 Asia/Shanghai heartbeat

E7 revised initial execution reached 239/267 records and remains alive with
16 workers. The initial-only driver has not crossed the strict verifier audit;
L2 remains locked.

E8 is unchanged at audited_incomplete, 254/267 strict valid scores and 13
unknown outcomes. It remains finished with no workers.

E9 has 87/89 task states while the evaluator remains alive. There are 42
completed and 43 initial_failed states, with two blocked infrastructure-budget
units. The evaluator is still processing the last initial work; no Ralph,
memory freeze, test, or vanilla phase has begun. No new run or budget reset was
performed.

## 2026-10-13 03:18 Asia/Shanghai heartbeat

E7 revised initial execution is at 222/267 recorded valid records. The
16-worker driver remains alive in `skill_rollouts`; initial audit, L2 input
validation, L2 and final frozen-library evaluation are still locked. The
supplementary verifier audit continues to identify the mteb-retrieve slot with
mirror DNS failure and no pytest outcome; it is not counted as a valid score.

E8 remains `audited_incomplete` with 254/267 strict valid scores, 197 ones,
57 zeros and 13 unresolved outcomes. It is finished and no new E8 requests are
launched. Workers are released; the execution audit and per-task report are
retained.

E9 remains in initial execution at 74/89 task states: 42 completed, 30
initial_failed and 2 blocked. The two blocked units, circuit-fibsqrt and
schemelike-metacircular-eval, exhausted their infrastructure retry budgets;
they are not model failures and receive no additional attempts. Among the
recorded states there are 40 reward-one and 32 reward-zero outcomes, plus two
missing RuntimeError outcomes. No Ralph rounds, frozen memory or test/vanilla
stage has started. Supervisor/evaluator are alive under the unchanged contract.

## 2026-10-13 02:11 Asia/Shanghai heartbeat

E8 runner finished with all 267 terminal trial results. Earlier 262-score
counts checked reward presence but missed eight verifier setup failures with
reported zero. Strict review accepts 254 scores (197 ones, 57 zeros); thirteen
remain unknown. No retries beyond the original policy are launched. Full
audit and E2 per-task comparison are saved in the E8 registration. E8 is
audited_incomplete, not accepted. See TB21_E8_MAX_1M_20261012.md for bounds and
failure details. Shared active reservations are now 80/250 (E7 16 + E9 64).

E7 continues initial-only execution. At the strict snapshot there were 190
recorded scores, of which 189 had actual verifier outcomes. Slot 51-2,
mteb-retrieve, has a reported zero after mirror DNS failure and no pytest
outcome. It remains unresolved for acceptance. The driver imported its old
validator before this patch; it is not hot-restarted. A supplementary
verifier_evidence_audit.json records the rejection. Full initial audit must
pass before L2. New/resumed processes validate old valid records again and
preserve rejected record metadata before retry, with original request counts.

E9 is making progress after cache permission recovery: 67 initial task states
(40 completed, 26 initial_failed, one blocked). All 26 teacher-candidate
failures have real raw verifier zero and tests_executed evidence. Two completed
RuntimeError states have no raw reward despite a framework reported zero;
they remain unresolved and are excluded from success/failure scoring. The
evaluator is alive; freeze, test and vanilla stages are pending. No budget reset.

Strict read-only review of the previously complete Final panels found gaps:
E3_FINAL 264/267, E4 262/267, E6_FINAL 261/267 under the new conservative
outcome policy. Each retains its historical audit and records and gets a
separate verifier_evidence_audit.json. All three reshard-c4-data attempts in
each panel end in Hugging Face dataset fixture errors. E4 additionally has
count-dataset-tokens 15-3 and mteb-retrieve 51-2 mirror DNS setup failures.
E6 additionally has count-dataset-tokens 15-2 DNS failure, mteb-retrieve 51-1
curl/uv setup failure, and portfolio-optimization 61-3 a pytest process crash
involving the agent's compiled extension. The latter needs attribution review;
it is not automatically classified as an infrastructure failure or model zero.
Old exact metrics are historical and must not be treated as fully accepted
under this stricter review. These Final panels are not restarted or rerun.

Validator regression checks passed remotely: 26 tests. Local pytest is absent.
E5_FINAL remains untouched and stopped. Automation continues.

## 2026-10-13 01:16 Asia/Shanghai heartbeat

E7 revised initial panel reached 175/267 unique accepted results. This check
reconciled all 175 record rewards with raw verifier rewards, task names,
nonempty trajectories, loaded body counts and Skill files; no mismatch was
found. The 178-card generation source audit remains passed. The original
16-worker driver remains alive; full initial execution audit and L2 gates
remain pending.

E8 has 260 real scores (196 ones, 64 zeros), five missing rewards and two
unfinished path-tracing-reverse trials. Missing values remain one
RemoteProtocolError and four VerifierTimeoutError, without filling zeros or
adding attempts beyond the original retry policy. Three pipeline cache
artifacts report version pipeline-uv095-py3139-v6, status ready, reused=false:
the bundles were transferred and unpacked, rather than reused in place.
Supervisor remains alive; final audit and E2 comparison are pending.

E9 stopped again with PermissionError reading an existing embedding vector
file owned by xinhaidong with mode 0600. The prior directory write fix did
not grant read access to those existing files. The owner granted read access
to its vector JSON files; all 104 current vector files are now readable by
liyishan. Prior recovery metadata was archived before the new recovery record.
Owned-child readiness passed; --resume uses the unchanged config and budget.
New supervisor is 2092370. At recovery there were 24 task states: 13
initial_failed, 10 completed and one blocked. These labels are not final
scoring. circuit-fibsqrt remains blocked after exhausting its infrastructure
budget; resume does not authorize additional attempts for that task.

Capacity was checked under the shared lock: 116 other workers plus E9 64
equals 180/250. E3_FINAL/E4/E6_FINAL are not rerun and E5_FINAL remains stopped.
All results remain closed-set; automation continues.

## 2026-10-13 00:16 Asia/Shanghai

E7 revised 178-card run reached 150/267 accepted initial slots. Read-only
verification checked every accepted identity, real reward, trajectory,
activation and selected body; no current error record remains at inspection.
The original 16-worker driver is alive. Initial full audit, L2 input validation,
L2 and final frozen-library evaluation remain pending.

E8 full job remains active: 255 completed results with real rewards (196 ones,
59 zeros), five missing rewards, and seven unfinished trials. All 255 score
candidates passed model, trajectory and actual HTTP max-effort/model/output
parameter checks. Missing results are mteb-retrieve (RemoteProtocolError),
two torch-tensor-parallelism and two pytorch-model-recovery trials
(VerifierTimeoutError). They are not reward zero and are not reissued beyond
the original Harbor retry policy. Three pipeline_cache.json artifacts exist;
their presence alone does not establish successful cache hits. Final audit,
complete metrics and E2 comparison remain pending.

E9 permission recovery is making actual progress: 20 task states versus 11
before resume. Nine states contain reward1, ten initial_failed states contain
real reward0 (two also have AgentTimeoutError), and one completed state is a
terminal RuntimeError without reward. That completed label is not a successful
task and must remain unresolved in final scoring. All ten teacher failure
inputs have actual verifier reward0; no infrastructure-only failure was found
among them. Supervisor 1024542, driver 1024543 and evaluator 1032332 are active,
with four current rollout children. Still in initial execution; Ralph/curation,
frozen-memory test and vanilla acceptance remain pending. No budget reset or
duplicate driver was needed.

Shared reservations remain E7 16 + E8 100 + E9 64 = 180/250. Completed
E3_FINAL/E4/E6_FINAL are not rerun; E5_FINAL remains user-stopped. Automation
continues until the authorized outstanding stages are accepted.
