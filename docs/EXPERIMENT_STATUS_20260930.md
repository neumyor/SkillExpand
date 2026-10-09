# Experiment status (2026-09-30)

This note records the latest SearchQA/ALFWorld campaign state. It is an audit
record, not a new benchmark measurement.

## Current predicted-val campaign

Campaign: `runs/f1a117f-predicted-val-cold-evolve2-20260929/`

The configuration used `acceptance_mode=predicted` and
`predicted_review_scope=val` (that option has since been removed from the code; predicted
acceptance now always scores the frozen val route). Train-task L1 outcomes were:

| Benchmark | Cold start | Evolve-1 | Evolve-2 | Artifact state |
|---|---:|---:|---:|---|
| SearchQA | 399/400 (99.75%) | unavailable | unavailable | cold-start audit passed; Evolve-1 stopped after one val-review failure |
| ALFWorld | 38/39 (97.44%) | 38/39 (97.44%) | 38/39 (97.44%) | all three stage audits passed |

SearchQA task `531` is in `val`, so its reviewer-format failure blocks the
predicted-val batch but does not belong to the 400-task train denominator or the
1,400-task test denominator. It is retained as a failed reviewer unit in the
campaign log.

The stopped continuation directory created during diagnosis was removed. The
original failed campaign remains intact for auditability.

## Historical final evaluations

Final-set numbers from earlier, separately frozen campaigns must not be mixed
with the current predicted-val campaign. The complete three-snapshot evaluation
in `runs/final-three-snapshots-20260927/` measured:

| Benchmark | Cold start | Evolve-1 | Evolve-2 |
|---|---:|---:|---:|
| SearchQA (1,400 test tasks) | 1101/1400 (78.64%) | 1084/1400 (77.43%) | 1098/1400 (78.43%) |
| ALFWorld (134 test tasks) | 115/134 (85.82%) | 115/134 (85.82%) | 116/134 (86.57%) |

These figures use the snapshot campaign's frozen routes and protocol. They are
reported for traceability and are not evidence that the interrupted current
campaign completed Evolve-1 or Evolve-2.

## Reviewer-format fix

`PredictedSkillScorer` now requests a strict JSON schema with exactly
`probability_true`, `predicted_success`, and `reason` (at most 80 characters),
while keeping reviewer thinking enabled. The parser accepts fenced JSON,
commentary around a valid object, trailing commas, and Python-style literals;
truncated output is rejected and the request is resampled (`reviewer.predicted_val`, up to 32 attempts; see `reliability/policies.py`). The regression suite covers these
cases in `tests/test_predicted_reviewer_format.py`.
