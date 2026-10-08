# L1 final learning (v5)

Retry planning and final learning are different operations. A retry state proposes
one next change. After every completed task, a separate synthesis reads the actual
action/observation pairs across attempts, including the final outcome. First-try
successes and exhausted failures follow the same path. An attempted action is never
promoted just because its text occurs in a successful episode.

`l1/protocol.py` owns repair state, factual evidence projection and bounded context.
`l1/learning.py` owns the final output contract, provenance checks and card creation.
The old conditional extraction and action-occurrence promotion branches are removed.
`l1/runner.py` persists retry reflections and final synthesis separately; completed
synthesis is reused on resume. There is no format-repair/retry mechanism in this change.

A final lesson is either a supported procedure or a conditional failure constraint.
Each instruction cites actual tool observations. A procedure requires a successful
attempt and substantive non-rejected cited observations; reference-only answer
acceptance is not method evidence. Model thoughts, task goals and nonexistent IDs
cannot establish a procedure. Citation counts are not artificially capped: a complete
procedure may need several observations. Diagnosis remains a hypothesis in a separate
field. No supported learning means an empty lesson, not an injected placeholder.
`learning.skill_body` returns executable text only for actionable card types.

These checks establish provenance and reject explicit negative evidence. They do not
prove arbitrary natural-language conclusions or causal necessity. A model may still
misinterpret an observation, omit a prerequisite, or overgeneralize; final empirical
evaluation remains necessary. Rejected synthesis is preserved with status `invalid`
and is not resampled. Transport failures leave synthesis pending: resume reuses all
completed trials and retries only synthesis. Neither changes the measured execution
result. Historical terminal `error` cards remain readable for audit.

Schema 4 adds `learning` and `extraction_status` while retaining outcome, evidence,
execution excerpts and provenance. Completed schema-3 cold starts remain readable by
L2; import does not revalidate their old lessons. Old unfinished L1 checkpoints fail
the v5 protocol signature, so new collection requires a new directory. The retired
`extract_on_mismatch` option no longer controls extraction.

## Benchmark isolation

ALFWorld alone loads `runtime/prompts/alfworld_contract.py` into execution, repair and synthesis.
The contract states actual TextWorld commands, inventory prerequisites, exact object
identity, reset, empty observations, budget and repeated-action termination. Its static
demonstrations use `move ... to ...`; its parser accepts bare move/examine/close/help.
The environment retains its repeated-action termination rule, reports `repeated_action`
and clears termination/truncation metadata at reset. Learning evidence strips repeated
admissible-action menus but preserves the complete action/observation and negations;
the actor still receives the menus.

SearchQA execution prompts, retrieval/scoring environment, model client and action
format recovery are unchanged. Its adapter only classifies evidence for the shared
new learning mechanism: Search/Lookup can supply substantive evidence; Finish reports
scoring feedback. No ALFWorld semantics are inserted into SearchQA prompts.

## Validation, 2026-09-23

- 123 offline tests pass, including regressions for a rejected action inside a successful
  trajectory, model-thought citations, direct-success synthesis, terminal failure
  evidence, placeholder exclusion, resume, and ALFWorld-only prompt dispatch.
- Ruff, compilation and diff-whitespace checks pass. Native ALFWorld reset/feedback smoke
  passes using the dedicated evaluation venv.
- SearchQA deterministic two-step execution before/after: every model input and the
  complete execution result are byte-identical. A separate AST/file audit confirms
  unchanged SearchQA execution methods, retrieval/scoring implementation, execution
  prompt module, agent factory and model client.
- Four real-model train units using qwen3.6-flash-distill: SearchQA 0 succeeds on its
  first attempt and 214 on its second; ALFWorld 0 and 19 both succeed on their first.
  Three final syntheses pass; SearchQA 214 is withheld because it labels scoring-only
  answer-format adaptation as a procedure. All four resume without new model requests.
  ALFWorld 19's new lesson describes actual countertop pickup, cleaning and move to
  the fridge, rather than the old unsupported bare-take rule.
- An initial development check exposed an unnecessary four-citation limit. It was
  removed and covered by a regression test; the final live check uses the uncapped
  protocol. This is not a full benchmark rerun or evidence of general success-rate gain.

Local, ignored artifacts: `runs/l1-v5-checks/` contains test logs, execution snapshots,
isolation audit, live checkpoints, raw requests, usage and the live summary.

## Second review and reproducible audit, 2026-09-23

The second review follows execution, retry, final synthesis, card publication and
resume. A truncated opening code fence (` ```json ` without a newline) previously
raised IndexError in both parsers. It now follows the existing invalid-output path;
no format recovery or additional model request is introduced. A separate test covers
resuming after synthesis is persisted but before the experience is published.

Run the offline audit before using experiment numbers:

```sh
PYTHONPATH=src python -m skillexpand.l1.audit \
  runs/<run>/discovery/trials/<task-id>.json \
  --config runs/<run>/config.json --output runs/<run>/l1-audit.json
```

Pass all expected checkpoint paths (shell globs are supported). Use the run's config
for custom adapters; without `--config`, the benchmark's default adapter is used.
This is an explicit audit command, not an automatic full-campaign completeness gate.
The caller must check expected task coverage and frozen campaign configuration/code
separately. Historical schema-3 cold starts are outside this v5 checkpoint audit.

The audit reconstructs action-linked evidence from trial events, checks reward and
trajectory accounting, re-parses saved synthesis, reconstructs semantic card fields,
and cross-checks the synthesis input/output against raw provider request logs. It
also checks unique request IDs, completed request pairs and provider token totals.
Duplicate events, modified evidence/lessons/verdicts, unmatched requests and incorrect
usage totals fail the audit. Missing provider usage or unfinished requests fail closed;
unknown tokens from failed requests are not estimated. Card tokenizer counters and
natural-language entailment are outside its scope.

Integrity, execution success and extraction status are separate report fields. An
invalid or transport-failed extraction yields no lesson but retains execution success;
resume reuses this terminal extraction outcome without automatically retrying it.
Context remains bounded: earlier evidence can be omitted, the count is reported, and
the last observation is retained. The four real samples audited here omit no evidence.

Second-review artifacts are `runs/l1-v5-checks/second-audit.json`,
`second-review-tests.log` and `second-review-snapshot.json`. The four saved real-model
units pass consistency and request/usage checks: three valid syntheses and one invalid
synthesis (SearchQA 214). These are re-audited earlier executions, not new real-model
experiments. SearchQA's deterministic actor input/output remains byte-identical to the
pre-refactor snapshot. Mutation tests demonstrate rejection of corrupted records.
