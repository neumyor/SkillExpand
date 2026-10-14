# TB2.1 Skill package research tools

This directory versions the research runners, offline analysis tools, tests and
protocol notes alongside the native `SkillEditor.propose_package` implementation.
The native package support lives in `src/skillexpand`; these tools do not replace
the regular experiment entry points in `scripts/`.

| Directory | Purpose | Recorded status at publication |
| --- | --- | --- |
| [generic_skill_evo_20261014](generic_skill_evo_20261014/README.md) | Native package generation, development execution, acceptance and artifact audit for the original single-task experiment | Completed; the archived report records a fresh 5/5 acceptance batch and the preceding failures |
| [three_task_input_ablation_20261014](three_task_input_ablation_20261014/README.md) | Fixed baseline/A/B protocol, historical-input filtering, completion audit and resource-use analysis | Fixed protocol complete; 63 executions, 12 generation slots; 51 valid outcomes and 12 unresolved outcomes retained |
| [library_pilot_20261014](library_pilot_20261014/README.md) | Shared three-skill library evolution with actual model routing, five development tasks and two independent regression tasks | Running; 262144 shared executor context, full legal evolution observations, 62 planned executions within the 66 cap |

These are source snapshots. Frozen inputs, source histories, generated packages,
SSE streams, Harbor jobs, binding snapshots and credentials remain in the separate
run archives. Audit documents describe the reviewed archive at their recorded
stage; copying those documents does not establish that a new run passed an audit.
The three-task final report preserves all source losses and unresolved outcomes.

The source snapshots retain the original cluster paths and execution settings.
Run them from a prepared run directory with its matching frozen native source,
runtime and inputs. Copying the source directory alone does not reconstruct a
measured experiment. Changes for a new environment require a new run directory
and pre-run review; the existing measured runs keep their frozen code.

From the repository root, run the self-contained regression tests with the
repository Python environment:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
PYTHONPATH=src:research/three_task_input_ablation_20261014 \
python -m pytest -q research/three_task_input_ablation_20261014/tests \
  research/three_task_input_ablation_20261014/test_completion_audit.py
```

Tests construct synthetic records and make no model or sandbox calls. They
validate the runner and auditor behavior; they do not complete the experiment.
