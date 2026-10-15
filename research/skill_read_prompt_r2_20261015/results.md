# Round 2 Prompt-Only Skill Reading Results

2026-10-15: the fresh reading acceptance gate PASSED, five of five. All five
official task rewards were 1. This establishes the required behavior for this
small batch, not future reliability or a causal task-performance improvement.
Round one's one pass, three order failures and one environment unresolved
remain preserved in `research/skill_read_prompt_20261015/results.md`.

## Intervention And Entry Points

Only the task-independent first-user-query tail changed. The prompt specifies
the next response's exact shape: one self-selected Skill `cat`, task_complete
false, then stop and receive the body before a separate response begins work.
The complete eleven-Skill catalog and mounted packages remain available. No
task-to-Skill mapping, selector, injected body, automatic reader, tool denial or
executor gate was introduced. The earlier optional-reading catalog wording
remains frozen; the tail explicitly overrides it.

`run.py` prepares this separate round and delegates phase execution, selection
and review to the unchanged round-one `run.py`, copied as `engine.py` in the
archive. The existing `launch.py` supplies credentials only through live process
input/environment. The selected text enters `load_manifest.json` as
`user_query_tail`; `research/loading_factorial_20261015/runtime.py` appends it
through `autonomous_query_tail`. `selection.json` preserves the exact used text;
`prompts.json` also retains unused v2. No global default was changed.

The eleven packages are the unchanged E6 final ten plus the initial Git
package. Direct byte comparison against round one found no changes in 341
source, library, task, runtime and judge files. Only prompts, round plan and
historical sandbox exclusions differ among the frozen inputs.

Executor remains requested `qwen3.6-flash-distill`, ModelBest `.co/v1`, context
262144, output 65536, top_p .95, top_k 20, thinking/preserve/interleaved enabled,
summarization enabled at 8000, with unchanged task environment and official
verifier. Provider response metadata says `qwen3.6-flash`; this does not verify
the provider's underlying alias identity.

## Every Execution

| Phase | Task | Slot | Reading/Order | Official Reward | Trial |
| --- | --- | --- | --- | --- | --- |
| v1 development | fix-git | 1 | Pass | 1 | fix-git__MbtksRR |
| v1 development | logs | 2 | Pass | 0 | log-summary-date-ranges__RcDbrFM |
| v1 acceptance | fix-git | 1 | Pass | 1 | fix-git__Dfdryir |
| v1 acceptance | logs | 2 | Pass | 1 | log-summary-date-ranges__KkdtG9j |
| v1 acceptance | fix-git | 3 | Pass | 1 | fix-git__9mGdPxg |
| v1 acceptance | logs | 4 | Pass | 1 | log-summary-date-ranges__XTkhF7u |
| v1 acceptance | fix-git | 5 | Pass | 1 | fix-git__HtRb5bD |

The preregistered early-stop rule selected v1 after both development reading
checks passed. v2 was never executed. Selection timestamp 1792074452.0963616
precedes every acceptance reservation; task rewards were not used to select.
Seven of the maximum nine executions were used, with no retries, replacements
or appended slots. All seven source sandbox identities are fresh and distinct.

In every execution, step 2 contains exactly one `cat .../SKILL.md`. Substantive
body lines appear in its observation and in the last user message of raw model
request index 1 (the second request). Task operations begin in step 3, after
that input. Git runs independently choose the generated Git package; logs runs
choose `terminalbench.family-p008`. The unchanged offline criterion requires
at least two substantive body lines totaling 120 characters and fails any read
batch containing task operations.

The development logs run really scored 0: it used 2025-07-13 instead of
2025-07-14 as the inclusive 30-day start. Reading is therefore kept separate
from successful use or useful Skill content. The new acceptance is descriptive
evidence, not a randomized comparison with round one.

## Audit And Archive

`final_audit.json`: audit passed, no errors, 1009 frozen file bindings,
seven executions, seven unique sandbox IDs, five acceptance reading passes.
Checks cover full catalogs and mounted bytes, exact query tails, absence of
initial body injection, effective runtime and actual request parameters,
official verifier artifacts, selection timing and the execution budget.
The focused tests previously completed with 13 passed; the judge and runtime
were unchanged in this round. Publication additionally runs `git diff --check`.

A staging failure preceded the valid round: the copied setup lacked
`audit.py`, and an attempted phase on that incomplete directory failed on
missing bindings before any slot reservation or model call. The directory and
failure log are retained separately as `skill-read-prompt-r2-20261015-preflight-zero-slots`.
This setup failure consumed zero executions; it is not omitted or replaced.

Complete local evidence:
`/Users/a1/Documents/New project/skill_read_prompt_archive/skill-read-prompt-r2-20261015`

Remote evidence:
`/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/skill-read-prompt-r2-20261015`

Raw requests/responses, trajectories, commands, body observations, trial
results, official verifier artifacts, snapshots, all frozen inputs and both
phase reviews are archived. The repository retains the protocol, preparation
entrypoint, exact prompts, selection and final audit for review.

E11 stays stopped. This round satisfies its reading gate; E10 was checked live
and remains running, so its complete archive is unverified. Initial-generator
content limitations and empty-resource scope also remain recorded. No code
push or successful reading result resolves those other prerequisites.
