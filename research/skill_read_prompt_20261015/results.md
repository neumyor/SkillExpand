# Prompt-Only Skill Reading Results

2026-10-15: acceptance gate FAILED. Five frozen-prompt independent slots yielded
one reading/order pass, three order failures and one environment unresolved.
No replacement or extra call was made. E11 remains stopped.

## Inputs And Entry Points

Preflight validated every body and materialized the full unchanged ten-Skill E6
final library plus the unchanged newly generated Git package: eleven entries.
Both public development tasks were frozen before calls: fix-git and
log-summary-date-ranges. Initial input contains full IDs/descriptions and the
common entrypoint path pattern; all eleven complete packages are mounted.
No external selector, task-to-Skill mapping, injected body, automatic reader,
tool rejection or executor hard gate was added.

Only the user-query tail varies. `run.py` prepares the frozen study, admits the
development/acceptance phases, passes the tail through `load_manifest.json` and
selects by reading evidence. The actual runtime entry is
`research/loading_factorial_20261015/runtime.py:autonomous_query_tail`, which
accepts a frozen `user_query_tail` and appends it after terminal state.
`evidence.py` judges returned substantive body and command ordering offline;
`audit.py` independently checks raw artifacts and frozen bytes.

Common executor: requested `qwen3.6-flash-distill`, ModelBest `.co/v1`, context
262144, output 65536, top_p .95, top_k 20, thinking/preserve/interleaved enabled,
summarization enabled at 8000, unchanged environment and official verifier.
Provider response metadata reports `qwen3.6-flash`; the requested alias remained
unchanged across phases. Do not infer an independently verified provider model
identity. Local Codex configuration is gpt-6.1-sol/high; root-session model
identity cannot be independently verified through an available switch API.

## All Nine Slots

| Phase | Task | Slot | Reading/Order | Official Reward |
| --- | --- | --- | --- | --- |
| v1 development | fix-git | 1 | Pass | 1 |
| v1 development | logs | 2 | Fail: no read before data inspection | 1 |
| v2 development | fix-git | 1 | Pass | 1 |
| v2 development | logs | 2 | Pass | 0 |
| v2 acceptance | fix-git | 1 | Fail: cat and Git operations in one batch | 1 |
| v2 acceptance | logs | 2 | Unresolved: environment TimeoutException | null |
| v2 acceptance | fix-git | 3 | Fail: cat and Git operations in one batch | 1 |
| v2 acceptance | logs | 4 | Fail: cat and task-data read in one batch | 1 |
| v2 acceptance | fix-git | 5 | Pass | 1 |

The preregistered rule selected v2 because development reading/order passed
2/2 versus v1 1/2. Official rewards were not used: v2's logs task scored zero
despite its reading pass. Selection was frozen before five acceptance calls;
acceptance did not change either prompt or library.

All four executable acceptance slots submitted actual Skill reads. However,
three combined reading with task operations before receiving the tool output.
Acceptance slot 1 submitted `cat .../SKILL.md`, `git status`, `git branch`,
`git log` and `git reflog` together at step 2. Slot 3 combined `cat`, status
and log. Slot 4 combined catalog listing, `cat family-p008/SKILL.md`, task
directory listing and `head` of a log. These fail even though body later appears
in model input. Slot 5 returned substantive Git body at step 2 / request index 1,
then began Git operations at step 3. Directory listing alone is permitted;
reading task data or inspecting Git state is substantive task work.

The unresolved logs slot failed during environment preparation before a model
trajectory existed. It consumes one slot but is neither a reading failure nor
a pass. Four observed official acceptance outcomes are all 1; the fifth is null.

## Frozen Final Prompt

> 本次任务必须先完成skill加载，再执行任务操作：从上方完整目录自主选择至少一项最相关的skill，在第一轮工具调用中读取 /opt/openclaw-skills/<skill_id>/SKILL.md 的实质正文，并等待工具结果返回。该轮仅可查看目录、定位和读取skill，不要同时提交检查或修改任务文件的命令。下一轮收到正文后再开始任务。无需读全库；可按需读取引用资源或继续加载其他skill。本要求优先于目录中的可不读说明。

This overrides the historical optional catalog text. That old wording remained
frozen because this experiment changed only the tail; its contradiction may
still affect behavior. No comparison isolates that effect. Prompt-only guidance
did not reliably produce the required two-turn read-then-act sequence.

## Audit And Limits

Final audit checks 1350 frozen file bindings, full eleven-entry catalogs and
mounted bytes, exact actual query tails, no initial body injection, common
configuration, effective parameters and official verifier results. Nine slots
were reserved; eight reached usable executions in distinct fresh sandboxes and
one failed environment setup. All failures and raw requests/responses are kept.
Focused tests: 13 passed, one dependency warning. `git diff --check` passed.

Full evidence archive:
`/Users/a1/Documents/New project/skill_read_prompt_archive/skill-read-prompt-20261015`

Remote:
`/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/skill-read-prompt-20261015`

The audit JSON retains commands, observations, matched body lines, returned
request indices and first task operation. This is a small two-task batch, not a
future reliability guarantee or a Skill usefulness estimate. No budget remains.
Further work would require a separately authorized experiment; plausible next
variables are removing contradictory optional wording or a dedicated read-only
first-turn instruction. Neither was introduced after final feedback.

E11 needs this reading gate, substantive initial-generator acceptance and E10
complete archive. This reading gate failed, E10 is still running, and initial
generator content limitations remain recorded. The combined start decision is
false regardless of code publication.
