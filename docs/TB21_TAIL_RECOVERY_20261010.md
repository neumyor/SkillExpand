# TB2.1 Tail Recovery, 2026-10-10

This checkpoint supersedes stale PID and credential-blocker descriptions.
Global reserved worker limit is 250. Four stopped drivers were recovered
after valid DeepSeek and Qwen completions through Tencent E2B. Desktop
DeepSeek and existing process Qwen credentials were transferred in memory.

| Stage | Valid slots | Workers | New PID |
| --- | --- | --- | --- |
| E1 empirical | 264/267 | 16 | 1336648 |
| E3 aligned | 263/267 | 92 | 1336955 |
| E5 aligned | 260/267 | 92 | 1337334 |
| E6 aligned | 261/267 | 50 | 1338283 |

All four were verified alive in full execution phase. Runtime reservation
reported 250. PIDs and counts are snapshots; read status and actual processes.

`scripts/resume_tb21_aligned_tail.py` reconciles earliest verifier-valid
requests before resuming identical run directories. Only 20 missing slots
are executed. Valid slots, libraries, canaries and budgets are preserved.
Each run records `tail_recovery.json` and `tail_recovery.log`. Credentials
were not persisted, and correction budgets were not reset. Earlier startup
failures were incomplete launch environments; both credential roles now
have verified valid completions.

E1 uses Qwen on the frozen repair9 final library. E3 uses DeepSeek for all
roles. E5 uses DeepSeek methods and Qwen execution. E6 uses DeepSeek cold
start and evolution, with Qwen selector and executor. E3/E5/E6 have not
entered L2: 267 valid slots and execution audit remain prerequisites.
Verifier timeouts and missing rewards remain unresolved. Agent timeouts
require actual valid verifier reward and activation evidence for acceptance.

E4 waits for audited evolved output from new E3. Scope remains 89-task
closed-set, three attempts, one evolution round. Additional final-library
empirical panels for E3/E5/E6 are outside this rerun.

## Pipeline verifier rerun supersedes timeout-zero scoring

The user requested real verifier rerun scores for E3 slots 81-1/81-3,
E5 slots 81-1/81-3, and E6 slot 81-2. Their five timeout-zero overrides
were moved to `retired_overrides` in `task_timeout_score_overrides.json`.
`apply_tb21_task_timeout_zero.py` must not reinstate those overrides;
it reconciles the earliest identity/trajectory/activation/body-valid real
verifier result instead. Raw timeout results and prior score entries remain.

Targeted driver: `runs/tb21-policy-tail-20261010T034929Z`, PID 3253018,
five reserved workers. Children at launch were E3 3253954, E5 3253955,
E6 3253956. Snapshot total reservation was 97/250. Read current status,
children and request activity before launching another driver. E5 81-3
waits for existing request 007; do not duplicate it. The other four slots
started fresh requests (E3 013/013, E5 008, E6 007).

The launcher uses `resume_tb21_policy_tail.py --pipeline`, at most three
new requests per slot this launch, with cumulative starting counts in
its manifest and no correction-budget resets. Active requests are awaited
before any retry. Completion refreshes policy_scoring.json, leaves null
rewards unresolved and keeps L2 locked until real 267-slot coverage and
execution audit pass. It does not itself start L2.

Task sandboxes configure PyPI/uv and Ubuntu/Debian mirrors at startup via
`tencent_tb2_skill_env.py` and `tencent_package_mirrors.py`. The selected
base is https://mirrors.tencent.com because mirrors.tencentyun.com does
not resolve in these E2B sandboxes. A 4 MiB torch wheel range took 1.05s
on the mirror versus 0.75 MiB in 15s on the original host. Dependency
versions and benchmark tests are unchanged. E5 81-1 request 008 was
directly checked for its /etc/uv/uv.toml mirror configuration.

The first four new pipeline slots exhausted three requests each before
agent execution because tmux was missing. Those RuntimeError records
remain excluded, with cumulative request counts preserved. The task-skill
environment now checks tmux and installs it through the configured apt
mirror when absent, before the agent starts.

After that infrastructure fix, a stopped-only recovery was launched in
`runs/tb21-policy-tail-20261010T035354Z`, PID 3284849, four workers:
E3 81-1/81-3, E5 81-1, E6 81-2. The first driver still waits for E5 81-3
request 007; do not start another request for that slot. Snapshot reserved
workers were 101/250. The recovery again allows at most three new requests
per slot in this launch; it does not reset cumulative requests or LLM
correction budgets. Inspect both control directories before further work.

The second recovery also exhausted its startup requests: the minimal
pipeline Ubuntu image has no CA certificate bundle, so HTTPS apt indexes
were unavailable and tmux had no installation candidate. A clean sandbox
smoke using the same image confirmed the missing CA file. With the host
system CA bundle installed, Tencent mirror apt update fetched 33.5 MB in
5s, tmux installed successfully, and `command -v tmux` returned its path.
The startup adapter now supplies the CA bundle only when absent, configures
mirrors, and checks/installs tmux before mounting skills and starting agents.

Current recovery: `runs/tb21-policy-tail-20261010T040001Z`, PID 3329200,
four workers, same four finished slots; first driver PID 3253018 still owns
the wait for E5 81-3. All previous infrastructure failures remain historical
requests. Each launch manifest records cumulative starting request counts.
All four new sandboxes were directly verified to have the Tencent PyPI
mirror configuration and `/usr/bin/tmux`: E3 requests 019/019, E5 request
014, E6 request 013. Final verifier rewards remain pending; L2 stays locked.

## Goal progress check: 2026-10-10

E5 slot 81-3 request 007 finished with AgentTimeoutError and a real verifier
reward of 0. Identity, trajectory, activation and mounted skill body passed
reconciliation. Its record uses the earliest valid real verifier result;
the retired fixed-zero history remains. Policy reports refreshed: E3
265/267, E5 266/267, E6 266/267. No execution audit exists yet; L2 stays locked.

Four pipeline slots remain active under PID 3329200: E3 81-1/81-3 request
019, E5 81-1 request 014, E6 81-2 request 013. Earlier driver PID 3253018
finished its E5 81-3 wait and owns no live reservation. Total reservation
is 4/250. This check launched no new requests. infrastructure_error slot
records describe historical failures while new requests remain unfinished.

This chat has an active goal to follow these five slots through real scoring,
report/checkpoint updates and execution audit. The sending chat will create
the hourly heartbeat; do not create a duplicate automation.

### Live sandbox inspection during active goal continuation

PID 3329200 remains live with four unresolved requests. E3 81-1 request
019 has generated ctrf.json and reward.txt in its sandbox: pytest reports
two passes and two failures (None position_embeddings in Llama attention).
Harbor result.json has not returned, so this observation is not yet accepted
as a scored slot and no reward was fabricated or manually copied into results.

E3 81-3 request 019 is in dependency installation; two test.sh/uv processes
coexist after verifier retry. E5 81-1 request 014 is downloading the uv
installer, with curl still live after about 333 seconds. E6 81-2 request
013 remains in agent execution with active Python processes and no verifier
files yet. This verifies a wait on live processes rather than a stopped run;
no request was duplicated or restarted during this inspection.

### Selector audit preflight and request update: 2026-10-10

Read-only preflight of all 89 tasks found disagreements between the three
selected family keys in already valid records. These are existing execution
results, outside the five-slot rerun scope; no valid slot was rerun.

| Run | Complete-task family disagreements | Task IDs |
| --- | --- | --- |
| E3 | 27 | 1,2,9,10,15,17,21,27,30,34,40,41,46,47,48,50,51,54,55,58,61,62,74,78,79,85,87 |
| E5 | 21 | 2,10,11,18,22,24,25,27,39,45,49,51,55,57,61,64,66,71,74,76,85 |
| E6 | 27 | 0,2,15,16,22,26,28,29,37,39,41,42,45,46,51,54,57,58,62,65,73,75,78,82,85,86,87 |

E3 task 81 also provisionally selected p003, p017, p003; its incomplete
records are excluded from the complete-task count. `collect_cards` rejects
independent selector disagreement instead of mixing families in one card.
Thus complete scoring alone cannot unlock L2 in any of these three runs.
The audit gate and family-card protocol have not been changed.

E3 81-1 request 019 returned VerifierTimeoutError with no usable verifier
reward, despite test files observed in the sandbox. It remains unresolved.
The existing driver automatically started request 020 within its three-new-
requests limit (019/020/021). Other latest cumulative requests remain E3
81-3=019, E5 81-1=014, E5 81-3=007 (valid real reward 0), E6 81-2=013.
Current total live reservation is 4/250. Policy scoring refresh still shows
265/267, 266/267, 266/267, with no active pipeline zero overrides.

Reconciliation additionally verifies the retired slot's directory/task
identity, selector catalog/key/load stage, and request-local mounted body.
Repeated statistics must retain the earliest fully validated real result.

Final check in this continuation: both E3 target slots have progressed to
request 020 after request 019 timed out without a valid verifier reward.
E5 81-1 remains on 014 and E6 81-2 on 013. PID 3329200 remains live;
no duplicate launch was made. Reapplying scoring twice preserved every
pipeline slot record and retired ledger entry, produced identical reports,
and kept active timeout-zero overrides empty in all three runs.
E5 81-1 sandbox directly confirmed `/etc/uv/uv.toml` points to Tencent
PyPI, with test.sh and uv running. All three execution_audit.json files
remain absent; 267-slot acceptance and L2 unlock have not been achieved.
Existing hourly automation tb2-1 targets this chat and reads this checkpoint.

## Skill-library attribution supersedes selector-disagreement blocker

The user explicitly confirmed that evolution is library/Skill-centric and
authorized the task-plus-actual-Skill grouping protocol. The prior requirement
that all three task attempts select one family is removed. The earlier
selector disagreement section is historical, not a current L2 blocker.

Implemented task-skill-v1 manifests, stable experience/card identities, raw
attempt/request/selection preservation, actual-family L2 batches, and exactly-
once original-slot provenance audit. External card consumption fails on missing
input and never starts L1. Legacy per-task cards remain compatible. Migration
archives prior derived cards and refuses old L2 plans/journals; no such plans
or journals currently exist in E3/E5/E6. Recovery uses experience IDs and the
frozen batch order. Read docs/TB21_TASK_SKILL_CARDS_20261010.md for the protocol.

Read-only grouping of current accepted real records passed source attribution:
E3 265 slots -> 121 cards, E5 266 -> 113, E6 266 -> 117. All accepted slots
are represented exactly once under their executed Skill. Full publication is
still locked by E3 81-1/81-3, E5 81-1, E6 81-2 missing real rewards. No
passing execution_audit was fabricated, no partial cards were published, and
no L1/model/sandbox request was issued by the offline checks.

When scores complete, use scripts/rebuild_tb21_skill_cards.py --publish on the
complete run. Then resume scripts/run_tb21_aligned.py with the existing run's
source/model/worker identity flags plus --l2-only. That entry audits all slots
before worker reservation or model relay and never runs new L1. Keep the 250
worker cap, existing correction budgets and original scorer/model roles.
The retired-zero reconciliation now reads initial_skills.json for aligned
runs (the actual frozen library), preserving the earliest valid real score.

67 focused grouping, aligned and serial-L2 checks passed locally. The code is
deployed on jinan40. PID 3329200 was still live at this snapshot; do not launch
duplicate pipeline recovery. Existing heartbeat should follow this updated
protocol and treat family differences as informational.

## Heartbeat snapshot: 2026-10-10 13:45 Asia/Shanghai

Scoring reports refreshed with earliest fully validated real rewards. E3
81-1 request 020 has a real verifier 0 (finished 13:18:56 Asia/Shanghai),
and E5 81-3 request 007 remains a valid real verifier 0. All retired pipeline
zero overrides remain retired, with no active timeout-zero overrides.

All three stages now have 266/267 accepted slots. Remaining requests:

| Stage | Missing slot | Latest request | Observed activity |
| --- | --- | --- | --- |
| E3 | 81-3 | 021 | trajectory/trial log updated within 81 seconds |
| E5 | 81-1 | 015 | trajectory/trial log updated within 26 seconds |
| E6 | 81-2 | 013 | trajectory/trial log updated within 2 seconds |

E3 81-3 request 020 finished at 13:30:11 Asia/Shanghai with
VerifierTimeoutError and no reward. The existing driver automatically moved
to 021, the third new request in this launch (019/020/021). No fresh launch
or correction-budget reset was performed by this heartbeat.

Driver PID 3329200 and all three stage children are still live. Total worker
reservation is 4/250. All three execution_audit.json and l2_manifest.json
files remain absent. Missing real rewards are the current gate; differences
between selected Skill families are informational under task-skill-v1.
No goal object is currently registered in this chat, and the operational
objective is not complete. Keep the existing heartbeat active.

## Heartbeat recovery: 2026-10-10, cached uv installer

E3 81-3 request 021 ended at 14:17:40 Asia/Shanghai with another
VerifierTimeoutError and no reward. Its previous recovery launch exhausted
019/020/021; the E3 stage child stopped. E6 81-2 request 013 ended at
14:31:07 with AgentTimeoutError but no verifier reward and remains invalid;
the existing driver started request 014. E5 remains on request 015. All
three reports still show 266/267. No zero overrides were restored.

Live sampling confirmed uv installer downloads still use GitHub and run at
about 36-55 KiB/s. PyPI/apt Tencent mirrors do not cover this installer.
The existing host archive at tbench2-openclaw-min/cache/uv/0.9.5 was reused.
New tencent_verifier_cache.py uploads it and installs a curl shim that handles
only the exact uv 0.9.5 installer URL on x86_64; other URLs use /usr/bin/curl.
The environment startup adapter enables the cache before agent execution.
No dependency version, task test, model role or timeout setting changed.

E5 request 015 and E6 request 014 were directly inspected and had not yet
started verifier commands. The cache was uploaded to both live sandboxes.
An isolated installer smoke in /tmp produced uv 0.9.5 and uvx 0.9.5 in
5.94s (E5) and 8.08s (E6), including upload. Existing requests were not
restarted. Python runtime and large torch/CUDA dependencies still download;
this fixes the uv GitHub step only, not every remaining download bottleneck.

After confirming E3's request had finished and child had stopped, a targeted
finished-only recovery was launched for E3 81-3 alone:
`runs/tb21-policy-tail-20261010T065558Z`, PID 191341, one reserved worker.
Its manifest records 21 prior requests, zero budget resets and at most three
new requests this launch. Latest request is 022; new sandbox initialization
and cache activation should be checked when its trial becomes available.
E5/E6 live requests were excluded from this launch. The older control PID
3329200 remains responsible for them. Total reservation is 5/250.

Scoring and task-skill-v1 gates remain unchanged. Missing real rewards still
block all three L2 stages. Keep following both controls; never mistake the
older control's E3 historical status for the new request state.

## Heartbeat snapshot: 2026-10-10 16:00 Asia/Shanghai

E3 81-3 request 022 formally finished at 15:58:45 with
VerifierTimeoutError and null verifier_result. Earlier live sandbox inspection
observed pytest finish in 21.26s (two passed, two failed) and reward.txt=0,
while a retried test.sh/uv command remained live. That sandbox observation
is not an accepted Harbor reward and was not copied into scoring artifacts.
The existing driver has started request 023, the second new request in its
022/023/024 launch budget; no additional driver or budget reset was made.

The uv 0.9.5 installer cache was confirmed in request 022. The cache shim
now installs each binary through a temporary file and atomic rename, avoiding
Text file busy when an earlier verifier command still runs. This shim update
was deployed to the three then-live sandboxes without restarting their
commands. It fixes installer replacement only; Python and package downloads
and overlapping commands after verifier timeout remain separate issues.

E5 81-1 request 015 failed without a valid reward and the existing driver
advanced to 016. E6 81-2 remains on 014. Current requests 023/016/014 have
recent trial/trajectory activity. Both control drivers and their relevant
children are alive. Worker reservation remains 5/250.

Policy reconciliation refreshed all three reports: each has 266/267 accepted
slots; the three remaining slots are E3 81-3, E5 81-1 and E6 81-2. Active
timeout-zero overrides remain empty. L2 stays locked pending formal real
rewards and complete task-skill-v1 execution audits. Keep tb2-1 active.

## Authorized full-cache handoff: 2026-10-10 16:14 Asia/Shanghai

The user approved enabling the independently validated complete cache
pipeline-uv095-py3139-v6 for the next pipeline recovery. Existing requests
E3 023, E5 016 and E6 014 are still running; no live sandbox was changed.
All three refreshed scoring reports remain 266/267 with no active fixed-zero
overrides. These live requests do not have the complete cache enabled.

Deferred controller: runs/tb21-pipeline-cache-wait-20261010T081409Z,
PID 578839, phase waiting_for_predecessors, zero reserved workers. Its
manifest records both predecessor controls (040001Z and 065558Z) and their
driver/stage PID start identities. It waits for those processes to finish,
including any remaining automatic retries in their existing launch budgets.
Do not launch another recovery while this controller is waiting or running.

The waiting process has TB21_PIPELINE_CACHE=1 and
TB21_PIPELINE_CACHE_VERSION=pipeline-uv095-py3139-v6. Credentials remain only
in the inherited live environment. Its exclusive cached-launch lock stays
held through the resulting recovery and automatic retries. Focused checks
covered valid-cache selection, zero-worker waiting, environment propagation,
predecessor waiting, reused PID handling and no-op completion. The live
controller's environment and held lock were directly verified.

After predecessors stop, the controller reconciles earliest identity,
trajectory and activation/body-valid real scores and excludes valid slots.
An unfinished latest request blocks dispatch. The existing pipeline launcher
then reserves only missing original slots under the 250-worker lock and
records cache version, cumulative request counts, zero budget resets and
at most three new requests per slot. The cache is injected by the existing
VERIFICATION_START hook with a 1200-second preparation limit; verifier
timeout stays 900 seconds. Cached launches keep the original model roles.

Follow status.json in the deferred control for launched_control, then inspect
that control's children, latest requests and pipeline_cache.json. The first
new verifier must show version v6 and ready, with transfer/unpack/preparation
times recorded. Check normal verifier output and pipeline_cache_downloads.json
for installer hit, pytest execution and download misses. No formal cached
request exists yet at this snapshot, so cache effectiveness on E3/E5/E6 has
not been claimed. Cache failures/timeouts/null rewards remain unresolved.

The existing supervisor refreshes policy scoring after recovery. Continue
checkpoint/report updates via tb2-1; unlock each L2 only after its 267/267
coverage and full task-skill-v1 execution audit. The validated engineering
reward is never imported into experiment slots. Timeout cleanup and result
collection behavior remain outside this handoff.

## Internal mirror DNS: 2026-10-10 16:35 Asia/Shanghai

The user authorized trying Tencent internal mirrors through DNS 183.60.83.19.
Independent same-image engineering sandboxes confirmed this DNS responds in
2-3 ms and resolves both mirrors.tencentyun.com and mirrors.tencent.com to
169.254.0.3. With that resolver, HTTPS torch index returned 200 in 0.177s
(290031 bytes), Ubuntu jammy InRelease returned 200 in 0.050s (270087 bytes),
and the public hostname on the same internal IP returned 200 in 0.283s.
These small-file probes establish connectivity, not large-wheel throughput.

Deployed tencent_package_mirrors.py now configures subsequent new sandboxes
with this DNS first, retains up to two original DNS servers as fallback,
and selects https://mirrors.tencentyun.com for Tencent package mirrors.
It writes /opt/tb21-package-network.json plus existing apt/uv/pip configs.
TB21_PACKAGE_DNS can select another resolver; an empty value opts out and
retains the supplied mirror and DNS. Focused checks passed internal mirror
selection, fallback DNS preservation and opt-out. No live experiment sandbox
or process environment was changed. The cached handoff remains waiting for
predecessors; future Harbor imports receive the updated startup helper.
Full pipeline v6 cache remains enabled for the queued recovery and is still
the primary way to avoid repeated multi-GiB dependency downloads.

## Heartbeat: 2026-10-10 16:55 Asia/Shanghai, E3 enters L2

E3 81-3 request 023 formally finished at 16:33:16 with no trial exception
and real verifier reward 0. Earliest valid result reconciliation accepted
its identity, trajectory and activation/body evidence. E3 now has 267/267;
E5 81-1 request 016 and E6 81-2 request 014 remain unfinished, each stage
at 266/267. No fixed-zero override was reinstated.

Published E3 task-skill-v1 cards offline: execution_audit.json passed with
267 valid slots, 267 unique provenance keys and 122 cards, no missing slots.
The prior derived card history was retained by the existing publisher.
E3 family-selection differences remain informational.

Started the authorized E3 --l2-only continuation, PID 722334, using its
original source/task identity, DeepSeek model roles and 92 workers. Its
status was directly confirmed running in family_evolution with 267/267;
l2_manifest.json and relay_manifest.json exist. No new E3 L1 was requested.
Total reservation is 96/250 (92 E3 plus 4 old tail controller); the cached
handoff PID 578839 still waits with zero workers for the E5/E6 predecessor
driver and stage children. The E3 predecessor driver and child have exited.
Do not duplicate either E3 L2 or the pending cache handoff.

E5/E6 latest trial logs were updated within 4/36 seconds and verifier stdout
remained empty during this snapshot. They have not yet supplied accepted
rewards. Once the old controller exhausts/completes its remaining requests,
the deferred v6 cached launch will reconcile and exclude E3 automatically.
New sandboxes retain the approved internal mirror DNS configuration.

The five-slot recovery objective remains unfinished with two missing scores;
E5/E6 L2 gates remain locked. No goal object is registered. Keep tb2-1 active
and inspect E3 L2 journals plus E5/E6 real verifier outcomes on next check.

## Heartbeat: 2026-10-10 17:59 Asia/Shanghai

E5 81-1 request 016 ended at 17:06:24 with VerifierTimeoutError and null
reward. Its stage child 3329575 has exited after exhausting this launch's
014/015/016 requests. Do not independently restart it: queued cache controller
578839 retains its exclusive launch lock and will dispatch after the remaining
predecessor finishes. E6 81-2 request 014 ended at 17:02:31 with
AgentTimeoutError and no verifier reward; it remains invalid. The existing
E6 stage child 3329576 automatically started request 015, the final request
in its 013/014/015 launch budget. Trial and trajectory updated within 36/37
seconds at this check. No duplicate request or budget reset was made.

Earliest valid real scoring refreshed: E3 267/267, E5 266/267, E6 266/267.
The two remaining slots are E5 81-1 and E6 81-2; active fixed-zero overrides
remain empty. E5/E6 L2 remains locked. No formal full-cache recovery has
started yet; deferred status remains waiting_for_predecessors, zero workers.

E3 PID 722334 remains running in family_evolution. Actual progress was checked
beyond PID: two l2_batches records exist (family-p001/p002), both Skill heads
have advanced to v1, and predicted score/review-budget/planner artifacts were
recently written. Relay log updated within 99 seconds; family-p003 planner
usage exists. This is ongoing L2, not completed evolution or final-library
empirical validation. Total worker reservation remains 96/250. Keep tb2-1
active; next inspect E6 015 completion, queued cached launch, and E3 journals.
There is still no goal object registered and the recovery objective is incomplete.

## Heartbeat: 2026-10-10 19:02 Asia/Shanghai

No new verifier score was accepted. Refreshed policy reports remain E3
267/267, E5 266/267 and E6 266/267, with no active timeout-zero overrides.
E6 81-2 request 015 is still unfinished and actively writing trial/trajectory
artifacts (23/27 seconds old at inspection). Its original agent timeout is
7200 seconds. No duplicate request was launched and budgets were unchanged.
E5 81-1 latest request remains the finished, invalid request 016.

Cache controller 578839 remains waiting_for_predecessors with zero workers,
as authorized: it awaits the E6 stage child and old supervisor before
dispatching cached missing slots. No formal full-cache request exists yet.
Do not infer complete-cache failure from the old uncached E6 request.

E3 L2 shows actual additional progress: four family batch records now exist
(p001-p004), and p001/p002/p003 Skill heads are v1. Recent p004 batch record
was written about 12 minutes before inspection; relay log updated within
159 seconds. E3 remains running in family_evolution, not complete. Total
reservation remains 96/250. E5/E6 L2 stays locked; continue tb2-1 monitoring.

## Progress: 2026-10-10 19:25 Asia/Shanghai

E6 81-2 request 015 formally returned at 19:09:03 with AgentTimeoutError
and real verifier reward 0. Earliest valid identity/trajectory/activation/body
reconciliation accepted it. E6 now has 267/267. Offline publication passed
execution audit with 117 task-skill-v1 cards and 267 unique original slots.
Started E6 --l2-only PID 1482469 with the original Qwen source, model roles
and 50 workers; no additional L1 request was made.

Only E5 81-1 remains missing (266/267). The deferred controller automatically
started runs/tb21-policy-tail-20261010T110923Z, PID 1370888, one worker,
E5 child 1371300. Manifest excludes E3/E6, records 16 prior E5 requests,
zero budget resets and a maximum of three new requests (017/018/019).
Latest request 017 is actively writing agent trajectory. Both driver and
stage environment were verified to have TB21_PIPELINE_CACHE=1 and explicit
pipeline-uv095-py3139-v6. Cache preparation runs after agent completion;
pipeline_cache.json does not yet exist, so formal cache hit/performance
remains to be verified. New sandboxes use the approved internal mirror/DNS.

E3 L2 remains running, six batch records exist and p001/p002/p003/p005 heads
are v1; relay log recently updated. Total planned reservation is 143/250
(E3 92, E6 50, E5 tail 1). E5 L2 remains locked. Keep tb2-1 active until
the last real score and its full execution audit pass; do not duplicate any
of the three live workflows or import engineering verifier rewards.

## Recovery complete: 2026-10-10 19:56 Asia/Shanghai

E5 81-1 request 017 formally finished at 19:46:41 with real verifier reward
0 and no trial exception. Earliest identity/trajectory/activation/body-valid
reconciliation accepted it. All E3/E5/E6 policy panels now have 267/267 real
scores with no unresolved slots or active timeout-zero overrides. All five
retired fixed-zero records remain retired, and request history is preserved.

Formal v6 cache evidence in E5 017: pipeline_cache.json is ready, transfer
809.43s, unpack 18.04s, total preparation 827.57s. Verifier ran from
19:45:13 to 19:46:40 (86.82s, below unchanged 900s); pytest reported two
passes/two failures in 33.95s. pipeline_cache_downloads.json reports stdout
available and zero downloaded packages. This proves formal complete-cache
compatibility and a real score, not a successful task solution. Cache transfer
is still paid outside verifier timing; no end-to-end zero-cost claim is made.

Published E5's 113 task-skill-v1 cards and execution_audit passed 267 unique
slots. E3/E5/E6 audits all pass with 122/113/117 cards respectively. Started
E5 --l2-only PID 1683226, original model/source roles and 92 workers; status
confirmed running in family_evolution with 267/267. E3 PID 722334 and E6
PID 1482469 continue L2. Total reservation is 234/250; no new L1 required.
E3 currently has six batch records and four v1 heads; E6 has one v1 head.

Cached tail and deferred controllers completed, with no extra 018/019 requests
needed. The five-slot scoring recovery and L2 entry gate objective is complete;
L2 evolution itself is ongoing and must not be described as complete or as
final-library empirical validation. No goal object exists to mark complete.
Stop tb2-1 as requested for completion of this recovery objective. This stops
the hourly recovery checks, not the three live L2 drivers.

## L2/final-panel follow-up: 2026-10-10 21:41 Asia/Shanghai

User re-enabled hourly tb2-1 for E3/E5/E6 L2 completion and authorized four
new frozen final-library panels: E4 transfer from E3 plus E3_FINAL/E5_FINAL/
E6_FINAL, each 89x3 attempts and 16 workers. E5_FINAL explicitly uses Qwen
selector and executor, unlike its initial DeepSeek selector. Keep the original
panels, journals and correction budgets; final evaluations never add L2 rounds.

E3 stopped after a provider stream ended without [DONE] in predicted task 9.
Confirmed its former PID 722334 was gone, checked capacity (142 workers), and
resumed the same --l2-only run as PID 2294751 with its original 92 workers,
source and process-only credentials. Status confirmed family_evolution. No
journal or budget was deleted/reset. E5 PID 1683226 and E6 PID 1482469 remain
running. Latest batch counts are 7/3/3, with recent relay activity. Total
reservation is 234/250. None has completed L2 yet; no final panel was launched.

Extended the shared empirical runner for completed aligned L2 sources and
multiple final heads, explicit authorized selector/executor roles, independent
credential routing, frozen snapshot/source checks, capacity-lock reservation,
earliest valid scored request reconciliation and unfinished-request exclusion.
Source status/result, execution audit, actual round journal replay and final
head identities gate preparation; E1 and historical E4 formats remain compatible.
Reports include initial/final per-task descriptive comparisons and disclose
the E5 selector change plus mirror/cache differences. See
TB21_FINAL_LIBRARY_EVALUATION_20261010.md for directory names and commands.
Focused empirical/aligned/task-skill/final-panel tests passed (52 checks).
The helper is deployed for future empirical processes; live L2 modules were
not reloaded. Wait for completed audited sources before freezing or dispatch.

## L2 progress: 2026-10-10 22:37 Asia/Shanghai

Verified current status, live driver processes, persisted batch records,
review-budget/usage timestamps and relay activity. E3/E5/E6 remain running
in family_evolution with 267/267 initial real scores and passing execution
audits. Recorded batches advanced from 7/3/3 to 10/4/4 against planned totals
20/11/10. Current heads include 8/3/4 v1 skills respectively; remaining
heads retain v0. Persisted batch reasons contain approved updates or holds,
with no invalid_review/invalid_hypotheses failure among recorded batches.

Live drivers are E3 2294751, E5 1683226 and E6 1482469; their reservations
remain 92/92/50 (234/250 total). Historical capacity snapshots contain older
PIDs and are not evidence of current occupancy. No restart or budget reset
was needed. None has a completed whole-round result, so E4 and the three
final-library evaluations remain gated; no partial library was frozen.

## L2 recovery: 2026-10-10 23:43 Asia/Shanghai

E3 and E6 remain active, with 11/20 and 5/10 recorded batches and 9/5 v1
heads respectively. E5 stopped at 4/11 batches (three v1 heads): predicted
validation task 81 received no visible answer from the provider despite
263403 reasoning characters. This is an L2 model-response failure, not a
Harbor verifier/download failure. Its task/body ledger retains initial_calls=1,
corrections_reserved=0 and reset_count=0; no budget was reset.

Confirmed the former E5 PID 1683226 had exited. Resumed the original E5
--l2-only identity with both original sources, 92 workers and credentials
inherited only in memory from the authorized live E6 launch environment.
An initial launcher exited before entering the driver because resolving the
venv Python symlink selected system Python; corrected to the absolute venv
path without resolving the symlink. No model request or budget change occurred
in that failed launcher. E5 PID 2965026 now reports family_evolution and
267/267. Its capacity lock verified E3 PID 2294751 (92) and E6 PID 1482469
(50), giving total reservation 234/250. Existing journals/cards remain intact.

All persisted batch reasons are approvals or holds, with no recorded invalid
batch reason. None of the three whole-round results is complete; final-library
panels and E4 remain gated. Keep the hourly follow-up active.

## L2 recovery: 2026-10-11 00:40 Asia/Shanghai

E3 stopped at predicted validation task 71 after the provider returned zero
visible content despite 9723 reasoning characters. Relay evidence also
records temporary upstream unavailability and output truncation. This was an
L2 model-response failure, not a verifier timeout or pipeline-cache miss.
The E3 driver had exited with 11/20 batches; existing review ledgers and cards
were retained. E5 continued at 4/11 and E6 advanced to 6/10.

Confirmed no live E3 driver, then resumed the same `--l2-only` run with its
original source, identity, 92 workers, and in-memory method credential from
the authorized E6 process. E3 PID 3218629, E5 PID 2965026 and E6 PID 1482469
now all report family_evolution and 267/267 initial coverage. No L1 execution
or budget reset was performed. Whole-round results remain incomplete; E4 and
the three frozen final-library panels remain gated.

## L2 progress: 2026-10-11 01:48 Asia/Shanghai

All three resumed L2 drivers are active in `family_evolution` with 267/267
initial coverage and recent relay activity. Persisted batch counts advanced to
E3 14/20, E5 6/11 and E6 8/10. No new provider failure, invalid batch reason,
completed round result or audit artifact was observed. Reservations remain
92/92/50, 234/250 total. E4 and all final-library panels remain gated until a
whole-round result, journal and execution audit pass for the source stage.
## E6 L2 complete and final panel launched: 2026-10-11 02:50 Asia/Shanghai

E6 completed its aligned L2 round: 10/10 batches, 117 experience cards,
267/267 source slots, result status complete, round-1 audit present, and
execution audit passed. Nine predicted updates were approved; unchanged v0
heads remain part of the frozen library. The released 50-worker reservation
made capacity for the authorized final-library panel.

Started `runs/tb21-e6-final-library-empirical-20261010` with the completed E6
source, 16 workers, Qwen selector and Qwen executor, pipeline cache v6 and
the existing internal mirror configuration. The process entered `relay_start`
with no model or verifier request yet. E3 remains at 17/20 and E5 at 9/11 in
active family evolution; their L2 inputs and budgets were not changed.
