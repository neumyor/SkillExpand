# E7/E8 verifier repair and recovery

## Scope and diagnosis

E7 acceptance uses only
`runs/tb21-qwen-deepseek-library-qwen-selector-20261011/revisions/178-cards-v1`.
The historical parent panel is not imported. E8 preserves its original job
`jobs/tb21_skillexpand_20261012/tb21_e8_deepseek_max_1m_raw_20261012`.

Missing outcomes are not all AgentTimeoutError. E7 has mirror DNS failure in
51-2, unavailable C4 verifier data in 73-1/2/3, and model-command
TimeoutException in 77-3. E8 additionally has dependency/setup failures and
VerifierTimeoutError. Framework setup reward zero is rejected without actual
pytest outcomes. AgentTimeoutError with real verifier evidence remains scored.

The historical bare Snapshot environment did not inherit the Skill package
mirror and uv installer setup. Existing pipeline v6 covered only
torch-pipeline-parallelism; it was not a general verifier dependency cache.

## Deployed changes

- Snapshot and Skill environments share idempotent DNS, CA, package mirror
  and cached uv setup, recorded in package_setup.json.
- QEMU bullseye security packages use archive.debian.org because the internal
  security mirror advertised packages returning 404.
- Opt-in task bundles are injected at VERIFICATION_START, after agent work,
  before the verifier timer. Tests and direct dependency versions are retained.
- Model command duration is 7200 seconds instead of 620. The provider's
  600-second socket idle timeout is distinct from total SSE duration. Harbor
  still enforces its original 7200-second agent budget and 900-second verifier
  budget. Cancellation kills the owned sandbox command.
- Recovery dispatch and audits preserve cumulative request/execution budgets.
  Saved agent.name is cleared so Harbor selects the original custom import_path;
  the adapter type is checked before reserving any execution.

## Cache validation

Root: `/data2/liyishan/tbench2-openclaw-min/cache/task-verifiers`.

| Task | Version | Same-image validation |
| --- | --- | --- |
| qemu-startup | repair-20261013-v2 | passed; injection 63s, original tests 10s |
| qemu-alpine-ssh | repair-20261013-v2 | passed; injection 42s, original tests 7s |
| mteb-retrieve | repair-20261013-v2 | passed; injection 744s, original tests 76s |
| torch-tensor-parallelism | repair-20261013-v2 | passed; injection 603s, original tests 109s |
| pytorch-model-recovery | repair-20261013-v2 | passed; injection 467s, original tests 76s |
| reshard-c4-data | repair-20261013-v4 | passed; injection 138s, offline fixture and original tests 25s |

Engineering runs have no agent solution. Their rewards are not benchmark
scores. Only manifest.status=ready can be enabled. Failed v1/v2 preparations
and canceled validation attempts remain preserved as diagnostics.

Enable only the required ready tasks via JSON `TB21_TASK_VERIFIER_CACHES`,
for example `{"mteb-retrieve":"repair-20261013-v2"}`. Set it to `{}` to disable.
Pipeline v6 enablement remains independent and unchanged. Mirror setup can
be disabled with `TB21_PACKAGE_MIRROR_URL=''`; this does not disable uv cache.
Do not hot-restart active trials to adopt new code.

C4 must cache the original allenai/c4 shard 00009 using the original
load_dataset call, data_files and train split. Mirror pagination links led
back to public Hugging Face; the current client uses httpx2. The mirror's
pagination then returned invalid JSON/403. A temporary desktop SSH proxy can
prepare original public-source data. The formal verifier must subsequently
load the cache offline in a fresh same-image sandbox; the proxy is not used
in formal evaluation. Hidden verifier data is injected only after agent work.

The original shard contains 356,318 rows. Version v3 failed the fresh offline
lookup because datasets 3.6 computes different configuration IDs from resolved
Hub URLs and the original data_files argument. Version v4 adds a relative
cache alias computed through BuilderConfig.create_config_id for that exact
argument; it references the same Arrow files without modifying the loader or
verifier. Version v3 stays unpublished and retains its failure evidence.
Version v4 passed in a fresh task image: the original call loaded all 356,318
rows with HF_HUB_OFFLINE=1 and HF_DATASETS_OFFLINE=1, then the unmodified
verifier executed actual pytest outcomes. The cache is ready. E7 C4 requests
002 were launched for 73-1/2/3 within the original remaining budgets.

## Recovery incident and budgets

The six E8 QEMU trials had one remaining execution after archive review.
The first recovery mistakenly retained agent.name=terminus-2 from result.json.
Harbor gives this built-in name precedence over the custom import_path, so the
wrong model adapter was selected. The owned recovery was canceled when this
was detected. All six outcomes are CancelledError with null reward; they
consume the six remaining attempts. This implementation error restored no
scores. Do not delete reservations, reset budget, or launch another attempt.

Evidence lives in E8 registration/infrastructure_recovery_20261013, including
each recovery_intent.json, result, trajectory and sandbox deletion record.
The factory fix was verified without dispatching a trial.

E8 strict coverage remains 254/267: 197 successes, 57 failures, 13 missing.
Its merged recovery audit is in registration/recovery_audit_20261013.
Original results and reports remain separate from the recovery report.

E7 77-3 requests 001-006 all ended TimeoutException without reward. No seventh
request is authorized. The four verifier slots retain a total cap of three
requests each. Recover 51-2 and 73-1/2/3 only with ready task caches and within
their remaining budgets. The standalone recovery launcher supports --slots.
It uses driver.pid and the shared 250-worker reservation lock. Initial source,
selection, activation, body and verifier evidence must all pass before L2.

E7 51-2 request 002 recovered a real reward of 1 with two passing original
tests, valid Qwen trajectory, selected Skill activation/body and ready cache
evidence (injection 459 seconds). Strict coverage advanced to 263/267.
The initial full gate still fails on the three C4 slots and 77-3; workers are
released and L2 stays locked. Do not rerun accepted 51-2.

Recompute `runs/verifier_recovery_budget_20261013.json` using
scripts/audit_tb21_recovery_budget.py. Retained recovery reservations count
even when cancellation produces no reward. E5 stays stopped; E9 and completed
Final panels are outside this repair.

## Verification and operations

Eleven focused local setup/cache/config/audit tests passed. Seven focused
remote adapter/config tests passed, including cancellation cleanup. A real
Harbor factory check selected TencentSandboxTerminus2 without agent execution.
Three Skill mounting/activation tests and two focused cumulative-budget tests
also passed remotely.
The six disposable same-image DNS/apt/uv probes passed. Cache manifests and
validation.json provide the separate dependency/test evidence above.

Run scripts/fetch_c4_fixture.py to fetch the original shard with resumable
ranges, copy the completed file to jinan40, then set TB21_C4_SOURCE_FILE to
that remote path when running scripts/prepare_c4_via_desktop.py. The original
Hub metadata and unmodified load_dataset call are retained; fresh same-image
offline lookup and original verifier execution are required before ready.
Alternatively run scripts/prepare_c4_via_desktop.py for direct preparation,
then use scripts/desktop_tb21_remote.py recover-e7-verifiers. Restrict slots
with TB21_RECOVERY_SLOTS=51-2 or 73-1,73-2,73-3. Credentials come from the
already authorized Desktop environment into process memory via SSH stdin.
Do not write credentials to logs/configs or copy credential files remotely.

Backups of the prior Snapshot and Skill adapters are in
`/data2/liyishan/tb21-tencent-skill/backups/verifier-repair-20261013`.
Roll back source only after owned executions finish. Never alter original
test.sh/task.toml, dependency pins, reward files or retained failure history.
