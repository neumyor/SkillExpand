# TB2.1 heartbeat checkpoint

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
