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
