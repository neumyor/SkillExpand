# Frozen Generic Skill Evolution Protocol

This restart uses a new remote run root:
`/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/generic-skill-evolution-20261014`.
The earlier targeted 5/5 result is historical evidence only. It does not accept
any candidate in this experiment.

## Authorized source input

The sole source is the original frozen `input.json`: public contract, original
skill, original E4/E6 experience cards, and all nine historical execution rows
in their original order. Missing E1 memory is not reconstructed. Historical
tool observations may include original PTY command/source echoes; these are
authorized original evidence. Stored assistant descriptions and diagnoses in
legacy memory remain claims, including when legacy fields call them observations.
No additional knowledge, old research candidate, checker, implementation,
diagnosis, acceptance feedback, or solution is added. Copied private task files
remain evaluator-only and never enter generation requests. Old artifacts remain
untouched.

## Mechanical projection

Before generation, freeze projection `tool-results-head-tail-8192-v1`. For every
historical or fresh development step with a tool call or observation, retain its original index and ID, ordered
tool function names, observation presence, and every ordered tool result content.
Exclude assistant message/reasoning, user narration, command arguments, and
semantic action rewrites. Retain strings of at most 8192 characters in full;
otherwise retain the first 4096 and last 4096 characters, along with original
length, head/tail lengths, and a truncation flag. Missing observations/results
remain explicit empty entries. No keywords, source markers, task names, checker
names, diagnostic prefixes, or success claims control selection. Rewards,
validity status, execution errors, and recorded exceptions are separate fields.
Fresh development feedback comes only from trajectories launched in this root.
No old feedback directories are scanned or imported.

## Manifest and candidate interface

Preparation writes `frozen/input.json`, `frozen/executor.json`,
`frozen/task_table.json`, copied runtime/task files, and `frozen/protocol.json`.
The latter contains `task_name`, `model_name`, `rollout` (runner script path
relative to the new root, transport and provider URL), and
`excluded_sandbox_ids`: all available original/old research sandbox identities
compiled from snapshot metadata without importing old research content.
Task identity is read from this manifest and checked against the one-row task
table. The full frozen executor model/request settings, environment, task
configuration, and verifier budgets remain unchanged, except the mechanically
required task directory and mounted skill root. A mismatch leaves the trial
unresolved; it is not recorded as reward zero.

Each generated candidate writes its complete serialized Skill to
`development/candidate-NNN/skill.json`, including body, resources, package mode,
identity, description, and provenance. Evaluation performs no generation and
injects no task-specific checker. It materializes precisely that full Skill.

## Development and acceptance

`evaluate.py dev --candidate N --batch B` launches three independent trials in
parallel and writes mechanical feedback. Repeat development under a new batch
number rather than overwriting prior trial inputs.

Before acceptance, independent audit writes
`development/candidate-NNN/preaccept_audit.json` with `passed: true` and a
`bindings` list. Every binding has `role`, `current`, and `snapshot`; paths are
relative to the run root or absolute. Snapshots are separate saved files with
the exact audited bytes. Required roles are `skill`, `frozen_input`, `executor`,
`protocol`, `evaluator`, `request`, and `generation_module`. The first five bind
the candidate Skill, frozen input/executor/protocol and running evaluator file;
the final two bind the actual complete generation request and generation module.
Add further roles for all imported generation helpers and prompt files. Audit
must inspect the complete effective prompt and module dependency content before
issuing the marker. Comparisons use saved bytes directly, without hashes.

`evaluate.py accept --candidate N --batch B` verifies every binding, freezes the
candidate Skill, marker and bound bytes, and launches exactly five fresh trials
in parallel. Existing batch bytes cannot change. All five must have valid
official rewards of 1, unique authoritative sandbox IDs, and no sandbox ID
shared with historical exclusions, prior development, or other acceptance
batches. Bindings are checked again after execution. A failed trial requires
development and a new complete acceptance batch. Existing complete trials may
be reused only when their frozen input matches exactly.

`evaluate.py report` aggregates only records and acceptance summaries in this
new root. A five-trial pass is an operational stability criterion for this frozen
task, not a statistical guarantee or proof of transfer. Raw token metrics and
elapsed durations are retained; monetary costs are not invented.
