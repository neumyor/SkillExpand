# Single-task generic package experiment

The source here records the `cancel-async-tasks` experiment using the native
`SkillEditor.propose_package` module from commit `255a67c`. The measured archive
is at `runs/generic-skill-evolution-20261014` on the original jinan40 checkout.
Its local mirror was `generic_skill_evo_20261014` beside the SkillExpand checkout.

- `generate.py` saves the native proposal request, streamed response and package.
- `evaluate.py` performs development and acceptance execution with audited inputs.
- `freeze_audit.py` binds candidate provenance, full resources and execution inputs.
- `audit_results.py` checks saved official outcomes and configuration independently.
- `report.py` renders the artifact audit; [report.md](report.md) records the completed experiment.
- `launch.py` is the original cluster launcher. It loads local environment files
  and sends credentials through SSH stdin.

[protocol.md](protocol.md), [genericity_audit.md](genericity_audit.md) and
[reproduce.md](reproduce.md) describe the original procedure and evidence limits.
Paths to `frozen/`, `development/`, `acceptance/`, `final_skill/` and JSON audit
outputs in those documents refer to the full run archive, which is not included
in this source snapshot. `generic_editor.diff` is superseded by the native module
already committed to this repository.

Execution also needs the archive's `code/skillexpand` source snapshot, frozen
runtime/task definitions and executor configuration, the repository's official
validator, and the original configured provider environment. Preserve those
inputs when reproducing the recorded measurement. The earlier experiment's
acceptance batches and retry history differ from the later fixed three-task
protocol; they must not be pooled into its results.
