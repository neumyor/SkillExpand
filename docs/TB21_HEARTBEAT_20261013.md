# TB2.1 heartbeat checkpoint

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
