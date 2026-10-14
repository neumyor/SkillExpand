# Fixed input audit

Read-only source audit; no model calls or task execution. Hidden verifier source,
assertions and evaluation stdout were not inspected. Prior final candidates and
their checker implementations are excluded from the source pool.

## Original source identities

Remote root: `/data2/liyishan/SkillExpand-tb21/SkillExpand/runs`.

| Label | Original run directory |
| --- | --- |
| E1 | `tb21-e1-empirical-final-library-20261008` |
| E4 | `tb21-e4-e3-final-library-empirical-20261010` |
| E6 | `tb21-e6-final-library-empirical-20261010` |

Each source is `slots/{task_id:02d}-{attempt}/record.json`, attempts 1–3.
The record identifies the exact agent trajectory and mounted historical SKILL.md.
Rewards below are existing record reward fields; no hidden evaluation output is
needed to select the original rows.

| Task | ID | E1 rewards | E4 rewards | E6 rewards | B failure rows |
| --- | --- | --- | --- | --- | --- |
| cancel-async-tasks | 7 | 0,0,0 | 0,1,0 | 0,0,0 | 8 |
| multi-source-data-merger | 52 | 0,1,1 | 1,1,1 | 1,0,1 | 2 |
| log-summary-date-ranges | 42 | 1,1,1 | 0,1,1 | 1,1,0 | 2 |

Cancel's existing frozen original is
`/Users/a1/Documents/New project/generic_skill_evo_20261014/frozen/input.json`.
Its provenance assembly is documented by the original
`cancel_async_skill_evo_20261014/research.py:prepare`.

## Memory lineage

The original E4-labeled memory is actually
`tb21-e3-e1aligned-free-library-20261009/discovery/results/{id}.json`, built from
DeepSeek E2 raw attempts. The original E6 memory is
`tb21-e6-e1aligned-qwen-experience-deepseek-evolver-20261009/discovery/results/{id}.json`,
built from older Qwen discovery attempts, not the nine final-library trials.
The E1 empirical run has no discovery results; do not silently replace its absent
memory with a progressive-run card.

E4 discovery reward vectors: task 7 = 0,0,0; task 42 = 1,1,1; task 52 = 0,1,1.
E6 discovery vectors: task 7 = 0,0,0; tasks 42 and 52 = 1,1,1.
Mixed aggregate cards can combine unsuccessful and successful implementation
clues. Even all-failure cards contain implementation and self-test claims. B
therefore drops every original memory card rather than guessing sentence lineage.

## Common initial skill boundary

Archive the original E6 attempt-1 loaded skill from the original E6 final-library
`library.json`: task 7 uses `terminalbench.family-p006@v1`; tasks 42 and 52 use
`terminalbench.family-p004@v1`. Do not substitute any later candidate.

The inspected p006 body gives generic PTY, QEMU, process shutdown and interface
rules. The p004 body gives generic artifact schema, normalization, transformation,
serialization and shell-hang rules. Neither contains a complete implementation or
identifiable task-specific target solution. This semantic observation does not
establish clean ancestry.

These are historical learned skills. In the E6 evolution run,
`evolution/round-1/cards/42--terminalbench.family-p004--v0.json` is successful,
and `52--terminalbench.family-p004--v0.json` has outcomes 0,1,1; both exact
experience IDs appear in p004@v1 provenance. The task-7 p006 source card has
outcomes 0,0,0, but its ancestral library and other source tasks also contain
historical knowledge. Keep the complete raw original skill and provenance in
source archives.

For all three tasks, `sanitize_seed` removes the historical body, resources,
provenance and unknown fields wholesale, retaining only the inspected generic
identity and routing fields. The replacement body is exactly
`Follow the public task requirements.`, with no resources and no package mode.
The same deterministic cleanup applies regardless of task, input body or arm;
it inserts no repair mechanism and requires no seed-generation model call.
Both A/B generation and baseline execution use this identical seed. Label the
baseline **sanitized-original**. It is a new comparison, not the old unchanged
baseline comparison. A historical evidence still includes the original allowed
memory and mounted skills; B does not inherit their success-derived body content.

## Frozen projection rule

A retains all nine original rows, their historical loaded skill text, original
memory experience cards, and tool results with the existing 8192-character
head/tail limit. Narration, reasoning and command arguments are not projected.

B retains only numeric, non-boolean reward-zero rows. It drops entire
success/unknown rows, all memory, all historical loaded-skill text, and every
historical result/argument/narration string. Only row IDs/rewards and step IDs,
indices, tool names, observation presence and structural counts remain. No
task-name, answer-keyword or semantic source-echo detector is used.

For both historical arms and all new development feedback, a case-insensitive
match of `/tests`, `verifier`, `reward.txt` or `test-stdout` (also the equivalent
underscore spelling) in any original command argument or raw result content
removes result content from the entire step, including sibling results, and
labels it `restricted_evaluation_output_removed`. Matching happens before
truncation. The projection never opens a referenced file. Development content
uses the same allowed-result policy in both arms, because independently produced
in-arm implementations are legitimate development feedback.

This is a bundled additional-history ablation conditional on a shared sanitized
seed and generic routing metadata, with historical learned body/resources removed.
Differences cannot
isolate answer leakage from lost failure diagnosis, lost memory, lost skill
context, or the unequal number of retained failed rows. A negative result does
not prove historical successful solutions never contributed.

## Exact frozen-artifact pre-run audit

The prepared task-level `input.json`, `seed.json`, `source_manifest.json`, both
`evidence-{A,B}.json` files and both `initial_request-{A,B}.json` files were
audited before model calls. `input_preaudit.json` records 155 passing checks.

Every historical row matches its archived original agent trace and mounted skill;
all source manifests identify only the preselected E1/E4/E6 panels. Existing
memory matches the original archived memory with the documented E3/E6 discovery
lineage. Cancel's raw `input.json` also equals the earlier generic frozen input
as parsed JSON. No prior research candidate or acceptance-answer path enters the
prepared evidence or initial request.

| Task | A rows / tool steps | B rows / tool steps | B retained result text | A user request characters | B user request characters |
| --- | --- | --- | --- | --- | --- |
| cancel-async-tasks | 9 / 70 | 8 / 65 | 0 | 173917 | 16365 |
| multi-source-data-merger | 9 / 66 | 2 / 12 | 0 | 200306 | 6036 |
| log-summary-date-ranges | 9 / 56 | 2 / 10 | 0 | 161150 | 5044 |

All six initial requests exactly match the native package prompt constructor,
the fixed generation settings and the frozen projected evidence. Their system
messages are identical across tasks and arms and equal the frozen system prompt.
Both arms' current-skill payloads are identical within each task, with the inert
body and empty resources. Baseline and initial generation read the same
task-level sanitized seed. Schema serialization omits empty legacy resources and
package-mode fields, and gives cleared provenance a new creation timestamp;
no historical source IDs, rationale or episode survives into that seed.

The local frozen-module `code/skillexpand/l2/editor.py` is byte-identical to
`src/skillexpand/l2/editor.py` at source commit
`255a67c4b71c24767265a142757443ec0b91dcda`. Development feedback uses the common
allowed-result filter rather than the historical-B structural-only mode.

No original historical step matched a restricted evaluation marker. Each of the
six A memory cards has exactly three matches, all the eight-character termination
label `verifier` (24 characters per card), rather than hidden evaluation output.
B contains none of those memory cards or any historical result text. This audit
made zero model calls and did not open hidden verifier source/assertions/stdout.
