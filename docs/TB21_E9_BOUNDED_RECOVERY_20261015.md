# E9 bounded recovery, 2026-10-15

The user approved recovery of the twenty pending Brew units within their original
eight request slots, and one independent physical dispatch for each missing
baseline: `reshard-c4-data` and `feal-differential-cryptanalysis`.

## Evidence and Compatibility

The original 89-task contract, 87 valid baseline outcomes, 67 complete ACPs,
failed requests, and unknown usage are preserved. Historical home paths are
mapped only while reading evidence. The only permitted contract difference is
the known config `source_path` relocation; cached request identity uses the frozen
original contract. No home compatibility links are created.

`recovery_amendment_20261015.json` snapshots the exact twenty units and original
failed records. Existing failed slots remain spent. Scoped markers allow the
nine historical generation failures to advance without relabeling their errors.
Completed responses replay with the original tool replies. No ninth slot exists.
New SSE streams and partial parsing diagnostics are saved next to each request;
invalid tools, incomplete streams and missing files remain failures.

## Entry Points

Run `scripts/resume_hkuds_e9_bounded.py --preflight` from the migrated project
with its Python/PYTHONPATH. This validates all baseline identities, real evidence,
and complete ACP replay with dispatch disabled and no model credentials needed.

The authorized Desktop memory bridge action is `hkuds-e9-bounded`. Its launcher
holds the shared capacity lock, checks strong identities and all old/new E9
children, verifies the original smoke, repeats preflight, and reserves 64 workers
only if the global total remains at most 250. A durable dispatch record prevents
automatic relaunch. Credentials stay in stdin/process memory.

The driver accepts `--bounded-recovery` and optional `--recovery-task TASK`.
`query-optimize` runs first; remaining tasks sort by remaining budget descending,
then name, with `break-filter-js-from-html` last. Teacher concurrency and relay
concurrency are one. The first new failure stops the whole run; no automatic
provider probing or retries are added.

Only after all twenty ACPs complete do the two missing baseline recoveries run
serially in `baseline-recovery-20261015/TASK`, with one physical attempt and zero
trial retries each. Completed original test output, raw reward and reward file
must agree. A selection sidecar points at accepted evidence; original rollout
files remain untouched. If either recovery remains missing the driver stops.
The new baseline outcomes feed normal Brew before original Curate/Ralph/freeze/
Serve89/Vanilla89 can proceed. Student concurrency remains 64, model and task
settings remain frozen, and test-stage teacher request count must not increase.

## Validation

Focused recovery tests exercise relocation-only contract comparison, frozen
request preservation, exact authorization markers, original eight-slot limits,
null SSE fields, truncated streams, redacted provider failures, retained raw SSE,
single physical recovery budgets, and Reader sidecar adoption. A real offline
preflight confirmed baseline89/valid87/success45 and complete ACP67 with zero
new model requests. E10 continues independently; E5 stays stopped.
