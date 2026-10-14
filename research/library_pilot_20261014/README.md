# Shared library pilot

This runner evaluates the unmodified E6 p003/p004/p006 seed as a complete library,
then evolves up to two complete-library candidates from actual selected
development executions. Every trial presents all three skill_id/description
entries to the native model selector before loading the chosen package.
Selection occurs once, in a separate model session; there is no dynamic
multi-skill switching inside execution.

All three skills are editable when their own selected development evidence
exists. Routing descriptions, names and topology are fixed. Development tasks
are cancel-async-tasks, headless-terminal, log-summary-date-ranges,
multi-source-data-merger and kv-store-grpc. Independent regression tasks are
constraints-scheduling and nginx-request-logging. No task-to-skill mapping is
written. Regression and final feedback never enter generation.

The pre-launch revision is recorded in [protocol.md](protocol.md). Baseline and
final each have seven tasks x three slots. Each of two candidates has five
development tasks x two slots: 62 planned executions, within the authorized 66
cap and 24 development cap. Four unused development slots stay unallocated.
Each generation round has one slot per eligible skill, at most six calls.
Failures, invalid choices, no_change and unresolved outcomes are retained.

All executor phases use input context 262144, output 65536, summarization enabled
with threshold 8000, and the E10 provider/sampling settings. Legal evolution
tool observations are passed in full. Neither 2000-character memory projection
nor 8192-character head/tail truncation is used. Restricted evaluation steps
are still removed, and provider overflow cannot trigger automatic trimming.

`prepare.py` verifies the original seed, copies unchanged task definitions,
derives a frozen executor configuration from E10 and binds source/settings.
`runner.py` performs single-shot selection/execution, groups evidence by actual
chosen skill and uses native `SkillEditor.propose_package`.
`runtime.py` preserves generic Tencent package loading and records effective
context plus mount paths. `audit.py` independently replays saved choices,
prompts and proposals, verifies official outcomes and renders the library and
regression report without model or evaluator calls.

Runtime records are stored separately in
`/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/library-pilot-20261014-262144`.
This source directory does not include credentials, histories or measured
results. New runs require their own prepared directory and frozen inputs.

Focused offline checks:

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src:scripts:research/library_pilot_20261014 \
  python -m pytest -q research/library_pilot_20261014/tests
```
