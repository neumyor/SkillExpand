# TB2.1 heartbeat checkpoint

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
