# Frozen Loading Mechanism Factorial Protocol

Date: 2026-10-15 Asia/Shanghai. This study freezes the original E6 three-Skill
library and the prior audited pilot's selected candidate-1 library. No evolution
or manual content repair occurs. IDs, descriptions, families and topology agree.

## Arms And Budget

Four cells: old_single, evolved_single, old_autonomous, evolved_autonomous.
Single uses one native external Qwen selector, then mounts only that package and
asks the executor to read its entrypoint, preserving the previous mechanism.
Autonomous has no external selector; every package is mounted under its stable
Skill ID. The initial executor instruction contains only the complete routing
catalog and where/how to read files. No body/resource content is injected.
The executor may read zero, one or multiple packages at any time using the
existing terminal. There is no task-to-Skill map and no task-specific advice.

Development smoke consists of exactly eight consumed slots: kv-store-grpc and
headless-terminal, one per cell. No replacement or additional smoke is allowed.
Smoke checks implementation paths, initial exposure and saved access evidence;
success is not a gate or tuning target. A genuine smoke implementation defect
may be fixed before formal freeze, with every consumed smoke retained.

After smoke, freeze all formal source, runtime, protocol, inputs, configuration,
schedule and scoring before any formal selection/execution. Formal uses the same
seven historical closed-set tasks, three repetitions per cell: 84 executions.
Total cap is 92 including smoke, no generation calls. Task order and cell order
are deterministically shuffled with seed 20261015, submitted in interleaved
blocks under 12 workers. No formal feedback changes source, prompt, config,
library or selection. No failed consumed slot is retried/replaced. Provider HTTP
and snapshot internal retries remain the common frozen runtime behavior.

Panel: cancel-async-tasks, headless-terminal, log-summary-date-ranges,
multi-source-data-merger, kv-store-grpc, constraints-scheduling,
nginx-request-logging. Smoke uses only development tasks. Regression labels are
retained for reporting, not candidate selection; no candidate is selected here.

## Common Conditions

Qwen qwen3.6-flash-distill and https://llm-center.modelbest.co/v1; context 262144,
output/max_tokens 65536, top_p .95, top_k20, thinking/preserve/interleaved enabled,
summarize enabled, threshold8000, agent timeout7200. Model settings and official
verifier are common across cells. Environment bootstrap, Python/tmux, network
mirrors and noninteractive verifier execution are identical across all arms.
No hidden tests, prior answers or previous solutions enter executor guidance.
Task definitions/verifiers are copied unchanged from the audited prior run.

## Evidence And Scoring

Retain catalog, native selection requests/SSE for single only, mounted package
bytes, effective config, full executor requests/responses, official outcome,
trajectory, terminal observations, unique fresh sandbox identity and snapshots.
An identical Linux inotify observer starts after mounting/setup and before the
executor loop. ACCESS establishes a file read, not semantic use. Catalog
visibility, body reads, resource-only reads, content visible in tool observation
and resource invocation are separate. File-name mentions are references only.
Invocation requires reviewing the actual command plus observed execution;
self-check PASS is not official task success. Preserve ordered accesses and
subsequent steps, including joint multi-Skill relationships. Never credit one
success independently to every read Skill.

Official reward 0/1 backed by reward.txt and real verifier test results is valid;
AgentTimeoutError with reliable verifier reward remains valid. Environment,
setup or verifier failure without reliable outcome is unresolved=null. Retain
every consumed failure. Protocol completion does not imply all slots valid.

Report all four cells, task/replicate outcomes, successes/valid/unresolved and
success-rate bounds [successes/n,(successes+unresolved)/n]. Only when relevant
cells are fully valid report point contrasts: loading effect for each library,
library effect for each loading mode, average main effects, interaction. With
unresolved results retain contrast bounds and avoid an exact rate claim. Report
both the whole panel and development/regression strata. Compare all cells from
new common-config runs; historical scores are background only. Small sample,
historical closed-set panel, no unseen transfer or stable causal claim. The
loading mechanism is an experiment variable, not Skill evolution benefit.

Structured-output reliability work is a separate offline change using stored
invalid responses. It cannot regenerate this study's libraries or explain its
score changes. Research Codex requested gpt-6.1-sol/high; record actual switch
verification honestly rather than treating the requested setting as evidence.
