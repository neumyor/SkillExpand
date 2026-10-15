# Initial Skill Package Mechanism Smoke

Completed 2026-10-15. Old and new initial Skills each passed both official
`fix-git` executions. This confirms the package generation/persistence/loading
path works; it does not establish improved task performance or transfer.
The separate 84-slot loading study was not started.

## Implementation

New TerminalBench cold starts use the actual `ColdStart.synthesize()` package
path: description, Markdown body and optional text resources. The program renders
one name/description frontmatter block and materializes `initial_packages/`.
Save/reload preserves package fields, and later edits preserve entrypoint format.
Historical manifests, explicit legacy mode, structured rules and other benchmarks
retain their previous format. Complete package requests that exceed the
conservative context budget fail before calling the model rather than clipping.
No task-specific logic was added to the modules. Memory, retrieval, executors and
verifiers were not changed by this implementation.

The prompt adopts triggering, progressive disclosure, operational guidance with
reasons and useful optional resources from the actual
[Anthropic skill-creator](https://github.com/anthropics/skills/blob/main/skills/skill-creator/SKILL.md).
The retrieved mutable-main snapshot is retained. This bounded experiment does not
implement that source's open-ended optimization loop.

## Frozen Generation Comparison

One existing one-task cluster, `tb21-e1-fix-git-smoke-20261002-v4`, supplied the
same legal input to both arms. The original experience card is preserved and
augmented only with public instruction and all eight historical raw tool
action/observation pairs. No model reasoning or hidden test contents were added.
The old card already contained truncation and model narration; the complete raw
source context survived the projection unchanged and takes precedence.

Both arms made exactly one DEEPSEEK_up5zdj generation call, with temperature 0,
output limit 65536 and context budget 262144. The two parsed evidence payloads
are equal. Both outputs passed schema, canonical persistence/reload and
materialization gates without manual edits.

| Output | Old | New Initial |
| --- | --- | --- |
| Body characters | 2256 | 3977 |
| Format | Eight numbered rules | Operational guide with frontmatter |
| Resources | Empty | Empty |
| Evidence references | No linked evidence | Raw-step and card references |

The new guide explains inspection, recovery, conflict resolution, checks and
stop boundaries. It still uses numbered subsections. Some citations refer to
card narration, and its claim that a trailing colon indicates a pager is too
broad. Stash/cherry-pick/diff alternatives are explicitly marked unvalidated.
These limitations were recorded before execution in `generation_review.json`.

## Four Executions

All four used autonomous loading with one visible catalog entry, no external
selector and no initial body/resource injection. The unchanged common Qwen
executor used `qwen3.6-flash-distill`, ModelBest `.co/v1`, context 262144,
output 65536, thinking preservation, summarization threshold 8000 and the same
official task/environment/verifier. Four workers ran the frozen interleaved
schedule without retries or replacement slots.

| Arm | Attempt | Official Reward | Body Read | Rounds | Tool Calls | Seconds |
| --- | --- | --- | --- | --- | --- | --- |
| Old | 1 | 1 | No | 9 | 17 | 228.6 |
| Old | 2 | 1 | No | 11 | 22 | 75.3 |
| New initial | 1 | 1 | Yes | 11 | 25 | 91.4 |
| New initial | 2 | 1 | No | 13 | 23 | 371.6 |

New attempt 1 issued `cat .../SKILL.md` at step 5; matching content appeared in
the terminal observation. It then merged the recovered commit, inspected the
conflict, resolved/staged/committed and checked status/history. This demonstrates
access followed by task actions, not causal credit for the Skill. The other
three successes occurred without observed body access.

The final offline audit passed: 323 frozen file bindings, two identical generation
evidence payloads, exactly four reserved slots/trials, four distinct authoritative
sandbox IDs, official outcomes replayed, unchanged generated libraries and
matching executor configuration. Every first user query ends with the exact
optional hint `你可以读取 /opt/openclaw-skills 下的 skill。`; every entrypoint
exists, with no full body/resource leak in the initial request. No unresolved
outcomes, audit errors or missing audit artifacts remain.

## Validation And Limits

Focused tests: 85 passed, 2 skipped (saved-response archive not configured),
2 dependency deprecation warnings. After restoring the original legacy fixture,
its focused file passed all 15 tests. `git diff --check` passed.

Both real generations chose empty resources. Synthetic tests validate resource
save/reload/materialization and script execution, but these live executions do
not validate generated resource invocation. Two preparation failures occurred
before any model call (staging Git metadata, then external source fetch); they
are retained separately. Historical source sandbox ID was unavailable in the
source export; new IDs are mutually distinct and excluded prior loading-study
IDs, and each execution used fresh snapshot creation.

The frozen generation/execution code predates final local compatibility fixes
for other benchmarks, strict body typing and preservation during L2 edits;
the effective TerminalBench generation prompt and generated packages were not
changed after generation. Root Codex `gpt-6.1-sol/high` switching remains
unverified; this is separate from the recorded generator and executor models.

Full local evidence archive:
`/Users/a1/Documents/New project/cold_start_package_archive/cold-start-package-20261015`

Remote evidence:
`/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/cold-start-package-20261015`

The archive contains source snapshot, frozen raw input, requests/completions,
unmodified old/new libraries/packages, trial trajectories, official verifier
outputs and `execution_audit.json`. `audit.py` performs offline replay only.
