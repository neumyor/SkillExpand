# Restart outcomes and limits

The module and system prompt were fixed before candidate 1 generation. The same
native L2 module produced candidate 2. No outcome prompted researcher edits to
that module, its prompts, input projection or feedback selection.

Candidate 1 generation used only the original frozen data. Its development
trial 002 passed and trial 003 failed officially. Candidate 2 generation used
those two new mechanical feedback records plus candidate 1's autonomous package.
The first development trial and candidate 1 acceptance results were not available
to that request. Candidate 2 output, including its checker, is entirely model
generated. Independent source reviews are recorded in `genericity_audit.md` and
`candidate_002_review.md` with exact binding markers under each candidate.

Candidate 1 acceptance batch 1 completed 4/5 and failed the complete-batch gate.
Candidate 2 development batches 1 and 2 each completed 3/3. Candidate 2 acceptance
batch 2 had three official successes and two unresolved initialization timeouts
at `apt-get update`; no model execution started in those two. All are retained.
Candidate 2 acceptance batch 3 completed 5/5 in five independent new environments,
with unchanged Qwen executor, environment, budget and official verifier.

After both corrected goal requirements were met, the remaining obsolete candidate
1 development trial 001 was actively terminated with SIGINT to its own Harbor
child. Its elapsed time was 2870.574 seconds. It is unresolved with
`ValueError: trial_exception:CancelledError`, not an official zero or success.
Its original result, trajectory and deleted sandbox snapshot remain archived.
No executor timeout parameter was changed. SSH transport had disconnected during
earlier trials, but remote processes continued; batches were never restarted or
partially replaced.

The final Skill is materialized directly from `acceptance/batch-003/skill.json`.
This restart does not reuse the old task-specific public replay/checker or claim
the old 210/210 as new evidence. New evidence is the complete official 5/5,
source/binding audits, fresh sandbox identities and 59 focused code tests.
Five successes meet the requested operational criterion on this task; they do
not establish statistical reliability or transfer to other tasks. Currency cost
remains unknown; raw generation and executor usage is preserved.

Delivery cleanup isolated the obsolete `frozen_evolution.cpython-311.pyc`
cache from the checkout, delivered code copy and remote code copy into historical
quarantine. Its source was already absent; no frozen source binding or experimental
setting changed. The copied package-editor test is included in `code/tests/`.
