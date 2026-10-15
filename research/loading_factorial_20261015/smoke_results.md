# Development Smoke Results

Eight declared slots completed on 2026-10-15. The frozen offline audit passed:
382 bindings, eight reservations, eight Harbor trials, no audit errors or pending
artifacts. Seven outcomes are valid: five successes and two failures. One outcome
is unresolved. No consumed slot was retried or replaced. No formal slot started.

| Task | Old single | Evolved single | Old autonomous | Evolved autonomous |
|---|---|---|---|---|
| headless-terminal | 1 | 0 | 1 | 1 |
| kv-store-grpc | 1 | unresolved | 1 | 0 |

The unresolved evolved-single kv-store-grpc trial timed out during the common
Python/tmux installation, before agent execution. It has no reliable reward.
Evolved-autonomous kv-store-grpc failed two official tests because the generated
protobuf request omitted the required `value` field. Evolved-single
headless-terminal received official reward zero with five failed and two passed
tests. These failures remain observed zeros, distinct from the setup timeout.

All three executed single trials read their selected SKILL.md. In both autonomous
arms, kv-store-grpc read no Skill and headless-terminal read family-p006 after
beginning task work. Kernel file accesses and terminal observations corroborate
the reads. No multi-Skill body read or mounted resource invocation was observed.
Thus smoke covers optional non-reading and later reading, but does not establish
multi-Skill use or resource execution. No library or loading-effect conclusion is
supported by these eight development executions.

The first launch failed before allocating any slot because Tencent relay
environment variables were absent. Its artifacts were retained separately with
zero reservations. The launcher now supplies the same public Tencent environment
settings as the prior pilot. Focused regression checks cover this configuration.

## User-Authorized Revision Before Formal Freeze

While smoke was running and formal consumption was zero, the user requested an
optional reading reminder at the end of the actual first executor user query:

> 你可以读取 /opt/openclaw-skills 下的 skill。

Both autonomous arms use exactly this sentence after Harbor's terminal-state
section. The directory contains the complete mounted library. Before execution,
the runtime checks the directory and every catalog SKILL.md exist. The formal
audit requires the sentence at the end of the first actual user message in
raw_model_requests.jsonl and checks the saved path-existence evidence. Single
arms retain their existing mechanism. Focused tests reject a system-only hint,
a hint followed by terminal state, and incomplete library evidence.

The eight smoke executions used the original prompt and remain unchanged. They
do not validate this new reminder. No additional smoke was run. The reminder
has passed offline checks but has not yet been observed in a live Qwen request;
that check remains required when formal execution begins. Formal has not been
prepared or launched as part of this smoke request.

## Evidence Locations

Remote root:
`/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/loading-factorial-smoke-20261015`

Local archive:
`/Users/a1/Documents/New project/loading_factorial_archive/loading-factorial-smoke-20261015`

Both contain `completion_audit.json`, `report.md`, frozen source/runtime/library
snapshots, and per-slot official outputs, requests, trajectories and access
evidence. The setup-failure slot has no executor request or loading observations;
the audit records those limitations explicitly.
