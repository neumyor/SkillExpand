# Library pilot protocol, revision 3

Revision recorded 2026-10-14T11:58:34Z, before any pilot execution. The original
panel had p003 read-only and kv-store-grpc in regression. No run was created,
no pilot executor/selector/generator call occurred, and no pilot kv-store-grpc
result was available or viewed before revision. Original historical tasks and
E6 seed ancestry remain closed-set evidence, not unseen generalization.

At 2026-10-14T12:07:00Z, also before launch, the user removed all evolution
input size projections. Legal observations are passed in full, with neither
the former 8192-character head/tail cap nor the generic memory projection's
2000-character entry cap and total budget. This runner supplies raw eligible
observations directly to the native package editor and never calls memory
projection. Provider context limits still apply; overflow is recorded as a
failed consumed generation slot, with no silent trimming or replacement call.

The unmodified original E6 p003/p004/p006 library is the baseline. All three
skills are eligible for native package editing when their own actual selected
development evidence exists. No skill is required to change. IDs, names,
descriptions, family identities, catalog topology, sorting and native selector
strategy stay fixed. There is no read-only skill and no task-to-skill mapping.

Development: cancel-async-tasks, headless-terminal, log-summary-date-ranges,
multi-source-data-merger, kv-store-grpc. Independent regression:
constraints-scheduling, nginx-request-logging. This split assigns evidence
eligibility, never skills. All seven tasks face the complete three-item catalog.
Each slot invokes native select_and_load with only public task text and the
skill_id/description catalog, records the original model selection, then loads
the selected package. One skill is selected once per trial in a separate
selector session; this is not dynamic multi-skill switching within execution.
Invalid selection consumes its slot, remains unresolved and has no fallback.

Baseline: seven tasks x three slots = 21. At most two complete-library
candidates, each evaluated on five development tasks x two slots = 10.
Thus planned development is 20, within the authorized 24 cap; four slots are
deliberately unallocated and cannot be used for recovery or extra acceptance.
Final selected library: seven tasks x three fresh slots = 21. Planned total
is at most 62, absolute authorized executor cap 66. Every reservation is
single-shot, including failed selectors, setup failures and interruptions;
no consumed slot is retried or replaced. Harbor n_attempts=1/retry=0. Existing
provider HTTP retries and snapshot retries are recorded separately.

Two generation rounds have three skill slots each, maximum six native
SkillEditor.propose_package calls. Round 1 uses only baseline development
evidence, grouped by the actual selected skill ID. Round 2 starts from the
first usable candidate library (otherwise baseline) and adds only that
candidate's development evidence, retaining loaded versions and exact source
slot paths. For a skill with no actual selected development evidence, skip
generation and keep it unchanged. no_change, invalid proposal and provider
failure consume their generation slot without replacement, keeping the current
skill. A candidate is usable only if at least one package changes effectively;
a duplicate/no-change library gets no development executions. Editable skills
are derived from the entire seed catalog, not a hard-coded subset.

No original regression trajectory, pilot regression outcome, final feedback,
previous research final package/checker/implementation, or manually authored
task-specific repair enters generation. The original seed is retained with its
declared historical ancestry; new evidence contains only public development
contracts, actual selections/loaded packages, recorded outcomes and generic
filtered tool observations. Whole tool steps mentioning /tests, verifier,
reward.txt or test stdout are removed as whole steps; all remaining observations
are retained at full length without any per-entry or total size cap.
All package generation uses the unchanged native generic system prompt.

Selection ranks usable libraries by official reward=1 count across their ten
fixed development slots, with earliest candidate breaking ties. Unresolved
remains null and provides no positive evidence; it is not converted to zero.
Freeze the complete selected library before final execution. No usable
candidate means no new-library final and an explicit no_usable_candidate
report. Final results cannot change selection, prompts, packages or budgets.

Every baseline/candidate/final executor has input context 262144, output 65536,
summarization enabled with free-token threshold 8000, 7200-second agent budget,
top_p=.95/top_k=20, thinking/preserve/interleaved enabled; provider
https://llm-center.modelbest.co/v1 and openai/qwen3.6-flash-distill. Selector uses
qwen3.6-flash-distill with the frozen native routing prompt. Generator uses
DEEPSEEK_up5zdj, thinking enabled, stream=true, max_tokens=65536 at the same
provider. Research Codex requested gpt-6.1-sol/high; root switching is not
independently verified. Context is a common condition, not evolution benefit.
Official task definitions/verifier stay unchanged. Fresh authoritative sandbox
identities must be unique and disjoint from recorded historical identities.

All run outputs, temporary files, new caches and logs go to the dedicated
/data2 run directory. Check actual resolved paths, free bytes and durable write
success before dispatch; do not delete unrelated data. Frozen byte snapshots
cover source, runtime, inputs, prompts and settings. Credentials stay in live
environment/SSH stdin. Independent audit records exact catalog/request/raw
selection, loaded content, mounted files, effective context, actual resource
reads/invocations, official results and all failures/source limitations.

Report old/new rewards for each task, selection frequencies, development,
regression and whole-library aggregates. Regression passes only when all
comparison outcomes are valid and each regression task's new success count is
at least its old count; a definite lower count is failure, unresolved comparisons
are uncertain. Neither activation metadata nor a successful outcome proves
resource correctness or causality. Final feedback never triggers another run.
