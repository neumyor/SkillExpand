# E9 Teacher Transport Recovery, 2026-10-14

The prior Brew run failed with provider_connectivity after an SSL bad-record-MAC
on the E2B command stream. Its follow-up launcher failed because pending teacher
requests deliberately block automatic replay. Neither prior process remains alive.

There are 35 durable original teacher requests: 6 completed and 29 pending.
One Brew ACP (fix-git) is complete. No Curate/Ralph/Serve/Vanilla result exists.
Pending requests have unknown provider outcome and unknown token usage.

The first recovery produced two more ACPs, then repeated the SSL bad-record-MAC
on shared HTTP/2 envd command streams; several streams had partial chunks.
E9 now configures the SDK envd transport as HTTP/1.1 inside the teacher driver
process only. No installed SDK/global adapter defaults or student subprocess
transport is changed. The model/provider request settings remain identical.

The recovery launcher now records recovery-http-NN.json under the shared lock
only after strong driver/supervisor and rollout-process checks pass. Each marker
binds the original request and records its spent turn. The runner preserves the
original pending request, skips that spent slot, replays completed responses and
dispatches only subsequent slots in the original range(8). No eighth-turn budget
reset or ninth request is allowed. Changed requests still fail validation.
Future transport failures persist status=transport_failed and unknown usage.

Two focused checks passed: failed-slot preservation and completed-cache reuse;
eight failed slots never dispatch a ninth. Source model, settings, prompts,
Write-only tools, official category timing, student results and contracts are
unchanged. The recovery is an execution adaptation, not exact Claude Code.

Corrected baseline scoring (verified from durable rollout evidence): 89 units,
87 genuine tests_executed binary rewards = 45 success and 42 failure. Two missing:
reshard-c4-data (NotEnoughSpaceException) and feal-differential-cryptanalysis
(AgentTimeoutError without verifier reward). Framework terminal_failure zeros
are excluded and neither trajectory is sent to Brew. Bounds: 45/89 to47/89,
50.56% to52.81%; valid-score rate45/87=51.72%. This remains incomplete.

No extra student attempt is authorized for either missing unit. Only baseline
configure-git-webserver retains its explicit third-attempt amendment.
Automation remains active; completed E7/E8/Final panels are not rerun and E5
remains user-stopped.

At 2026-10-14 07:00 UTC, the HTTP/1.1 recovery had produced 27 complete
Brew ACPs out of 87 eligible baseline units. The preceding driver stopped on
upstream HTTP 503, "Request buffering capacity is temporarily exhausted".
The scoped launcher now sets TBENCH_RELAY_MAX_CONCURRENCY=4 while retaining
16 teacher threads, 64 student workers and the 64-worker reservation. Both
new supervisor and driver strong identities were verified alive; the live
driver environment confirms relay concurrency 4. This limits transport load
without changing prompts, model parameters or the original eight-turn budget.

Only RelayError transport_failed records and original pending records qualify
for recovery markers. circuit-fibsqrt/http-02.json has error_type=ValueError
and no marker; its stream did not finish with stop/tool_calls. The finish reason
and token usage were not preserved by the original failure recorder and remain
unknown. headless-terminal/http-00.json is teacher_failed/TypeError with no
marker. These generation failures are unresolved and must not be automatically
resampled or silently reclassified as infrastructure. Curate, Ralph, Serve and
Vanilla have no results yet. No unit has been granted a ninth teacher request.

At 2026-10-14 08:00 UTC the supervisor and driver were both dead, with
35 complete ACPs, 45 started units and no remaining reservation. The recorded
failure was an actual extra Write path, not path normalization: cached responses
requested /app/solve.py, /app/solve_task.py, /app/verify.py and /app/check_env.sh.
These remain rejected. Another unit, llm-inference-batching-scheduler, has an
incomplete-stream ValueError. No generation-failure recovery marker is allowed.

The --brew-unstarted-only mode advances only eligible units with neither an ACP
nor any original http-NN.json. It excludes all previously attempted units, does
not create recovery markers, and uses the same eight-turn ceiling. It records
its scope, individual failures and missing ACPs, then exits before Curate even
if its selected units finish. The first scoped selection contains 42 untouched
units. Existing generation failures and the two student missing scores still
block complete acceptance. Three focused recovery/scope tests pass.

The user subsequently authorized removal of the extra-Write failure gate.
Official Brew requests three ACP files but its extractor ignores other files;
the adapter's previous hard failure was stricter than that upstream behavior.
Extra Write calls now stay in the original HTTP response and tool history while
only the three ACP paths are collected. Cached completed responses replay with
the same tool acknowledgement, preserving later request identity. Extra files
are not mounted into student guidance. Invalid content and incomplete ACPs still
remain failures. Brew records per-unit errors and completes other eligible units
before its completeness check, so one failure no longer cancels queued work.
No teacher budget or baseline retry allowance changes. Four focused tests pass,
including extra-Write cached replay without another model request.
