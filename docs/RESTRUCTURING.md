# SkillExpand source reconstruction

The repository was reconstructed from commit `38f5c530c1361fc79e3e87adb56d14ecc304e610` on 2026-09-23. The published main branch starts with a clean source snapshot. The original repository history and pre-migration source were backed up outside the repository before reconstruction. ExpeL attribution and the Apache 2.0 license remain in NOTICE and LICENSE.

## Code boundaries

All importable code now lives under `src/skillexpand/`. L1, L2, evaluation, runtime, persistence and benchmark environments have separate packages. `pyproject.toml` declares the installable package, dependencies, packaged defaults and `skillexpand` entry point. Both `python -m skillexpand` and the installed command use the same CLI.

The inherited executor remains under `runtime/` to preserve behavior. SearchQA's local context environment and shared QA prompts were extracted from their former HotpotQA locations. FEVER/WebShop environments, unused benchmark configs/data, upstream images, and the retired online L3 updater/pool were removed. Historical record schemas and score-reading primitives remain for existing artifacts.

ALFWorld is an optional dependency; SearchQA imports and executes without the simulator installed. ALFWorld prompt demonstrations, execution budgets and repeat-action behavior are preserved. The existing empty-container observation fix is retained.

## Data and historical experiments

Datasets and run artifacts are no longer tracked. The data preparation script downloads the same ALFWorld assets from a pinned upstream commit, independently of this repository's history. It also accepts an existing archive, records its SHA-256, reuses identical files and refuses to overwrite conflicting assets.

Local task manifests, cold starts and experiment outputs remain in place. The data script prepares upstream assets; it does not reconstruct custom train/val/test assignments. Pass the original task file when reproducing an experiment.

Completed cold starts retain byte-identical configs and cards. Their old `skill_evolution.l1.adapters:*` references are resolved at the adapter-loading boundary without rewriting saved inputs. Python imports and CLI invocations should use `skillexpand` going forward. An already-started L2/test run has a different implementation fingerprint: import its original cold start into a new run directory instead of mixing implementations under `--resume`.

The new implementation fingerprint includes the entire package, including environment wrappers, model clients and prompts. Environment changes therefore invalidate execution protocols as well as L2 changes.

## Verification

- Before migration: 107 existing tests passed.
- After migration: 111 offline tests passed. Two tests for the removed online L3 pool were retired; six migration/data/observation regression checks were added. All remaining previous tests are preserved.
- A fresh Python 3.9 virtual environment installed the regular package with declared dependencies, without ALFWorld. The regression suite and installed CLI/config loading were checked independently of the editable development installation.
- Spawned process workers successfully loaded the new module paths and packaged configs.
- Both historical cold starts loaded successfully: 400 SearchQA cards and 39 ALFWorld cards. Task-table, initial-library and card hashes matched the pre-migration implementation exactly.
- Active baseline prompt tables, initial agent prompts, the L2 reviewer system prompt, and a real native ALFWorld observation/reward/termination/step result matched before and after migration.
- The dedicated ALFWorld Python 3.11 environment passed the native two-reset adapter/card smoke test. No model request was needed for that environment check.
- The pinned ALFWorld archive extracted 2,219 files; all 2,218 environment assets matched the previously installed files byte-for-byte. The remaining file is the upstream task manifest.
- Python compilation, static undefined-name checks, documentation links and staged whitespace checks passed.

These checks validate reconstruction and execution compatibility. They are not a new full benchmark experiment and do not revise the previously reported success rates.

## 2026-10-08 cleanup (after merging reviewer-coevolve and model-roles)

### Freezing and provenance

- `persistence/io.freeze` keeps every frozen field strict except the `code` source fingerprint. A code-only difference raises `FrozenCodeChanged`; `--allow-code-change` (CLI and `scripts/evaluate_snapshot.py`) resumes and appends the drift to `code_changes.jsonl` beside the frozen file. Frozen manifests are never rewritten. Previously every code change made `--resume` impossible, which led to the archived re-signing scripts. L2/discovery prompts and acceptance logic are covered only by the code fingerprint, so changing them remains a protocol change that requires a new run directory; the flag is meant for fixes that do not change model inputs or decisions. Because this cleanup moved most modules, runs started before it should be finished with their original code where possible.
- The campaign launcher moved from `scripts/run_campaign.py` into `skillexpand.campaign`. `prepare` writes a frozen launcher `<root>/code/run_campaign.py` that imports only `<root>/code/src`. `verify` no longer compares the live checkout's Git state; `check` reports it as `source_drift`. The preflight checks of `check_fresh_campaign.py` became the `independent-check` action and now run under frozen code, not the live package. `scripts/run_campaign.py` now only performs `prepare` itself and hands every other action to the campaign's frozen launcher. Campaigns prepared before this change keep their own frozen `run_campaign.py` and must still be driven from the checkout that prepared them: the shim and the new `check_fresh_campaign.py` detect such campaigns and refuse instead of mixing code versions.

### Layout

- Base helpers live in `persistence/io.py`: atomic `save`, `freeze`, `read_split`, `read_jsonl`/`append_jsonl`, `RunLock`, `exclusive_lock`, `require`, `code_signature`. `runtime/json_output.extract_json` replaces `l1.family_discovery._extract_json`; parse failures raise `ResponseFormatError` (a `ValueError`, like the former `DiscoveryError`).
- Moved modules: `persistence/artifacts.py` -> `l1/artifacts.py` (`provider_signature` -> `runtime/models/llm.py`); `l2/patterns.py` -> `l1/patterns.py`; `l2/structured_skill.py` -> `structured_skill.py`; `l1/alfworld_contract.py` -> `runtime/prompts/alfworld_contract.py`. Pool workers moved out of `runtime/parallel.py` into `l1/workers.py` (`ExperienceSpec`, `execute_experience`) and `evaluation/workers.py` (`FixedSpec`, `execute_fixed`). Benchmark helpers moved from `runtime/utils.py` into `benchmarks/`. `SkillSelector(host, adapter)` takes the adapter explicitly instead of reading `host.l1_adapter`.
- Package layers import downward only; `tests/test_layering.py` enforces this, including function-local imports.
- The CLI test phase and the snapshot tool share `evaluation/snapshots.evaluate_library`; the protocol is frozen before routing, as before. The snapshot tool loads frozen test routes read-only and never writes below the training run. A failing route group no longer stops the remaining groups; `progress.json` records completed and failed groups. `evaluate_snapshot.py` accepts `--round 0` (cold-start library) and now writes `skills/<id>.json` (previously unreachable code). Its `protocol.json` no longer contains `test_workers` or `library_hash`, so snapshot directories produced by the old script cannot be resumed with the new one.
- One-off launch/repair scripts tied to specific campaigns were moved unchanged, apart from a run guard, to `experiments/<date-campaign>/`; see `experiments/README.md`.

### Removed code

- ExpeL trajectory retrieval, critique/rule learning and reflection loop (`ExpelAgent`, `ReflectAgent`, `runtime/memory`, `runtime/embedders.py`, the retrieval/critique prompt registry entries and agent config keys), `run_executor_once`, interactive `testing`/`input()` paths and the unused `long_context_llm`. `RepairAgent` now subclasses `ReactAgent`; `build_agent` requires `agent_cls`. A provider `InvalidRequestError` now propagates instead of calling `input()`, which raised `EOFError` in workers and could block an attached terminal.
- Unused `runtime/utils.py` helpers; the retired patch/meta/selection-attempt records and their logs; the unused routed `execute`/`execute_routed`/`UnitSpec` path; `canonical_patch_body`/`patch_hash`; dead parameters of `SkillEditor.propose`; `scripts/smoke_agent.py`, which already failed on a missing function.
- Ruff now runs all pyflakes checks (`F`), excluding `experiments/`.

### Fixes

- After a failure, `summary.json` reported `completed_batches=0` because it counted pre-round batch IDs. It now counts the batches of the failed round and records `evolution_round`, including failures during that round's L1.
- `load_cold_start` raised `UnboundLocalError` when `discovery/card_hashes.json` was absent (pre-existing).
- `append_jsonl` adds a missing final newline before appending, so a ledger is never merged into one corrupt line.
- `read_jsonl(..., repair_tail=False)` is side-effect free (it used to append a missing final newline). Reviewer feedback/update ledgers share this implementation; audits read them without repair.

### Verification

- 237 offline tests pass (the 10 `embedders` tests and 1 retired acceptance-rate test were removed with their code; 16 layering, freeze-policy, campaign-launcher, snapshot and regression tests were added). The native ALFWorld smoke (`EXPE_NATIVE_ALFWORLD_SMOKE=1`) passes under the dedicated ALFWorld interpreter, and spawned-process pools import both worker modules.
- Offline behavior fingerprints with scripted model outputs were identical before and after: every prompt sent to the model, the L1 checkpoint and the experience card for a SearchQA fail→reflect→success task and a native ALFWorld task (the dedicated ALFWorld interpreter), single-attempt fixed-Skill execution on both benchmarks, selector prompts, and a full offline L2 round (all Planner/Editor/Reviewer prompts, `l2_batches`, `l2_proposals`, `skills.jsonl`), compared against the pre-cleanup tree in the same run directory.
- These checks establish behavioral equivalence of the executed code paths. They are not a new benchmark measurement.

## 2026-10-08 unified failure handling

All failure handling moved into `src/skillexpand/reliability/`; see `docs/ERROR_HANDLING.md`.

- One taxonomy (`errors.py`) with six categories -- infrastructure, response, provider_rejected, integrity, configuration, bug -- and one disposition table. Third-party exceptions are translated at their boundary through a registry (`register_translation`); anything untranslated is a bug.
- One table of retry/repair budgets (`policies.py`) and two retry loops (`retry.py`). Existing budgets are unchanged (provider: unlimited; predicted/calibration Reviewer: 32; Planner and card Reviewer: 2; discovery: 3).
- Behavior changes (failure paths only; successful outputs are unchanged):
  - An exhausted repair budget is now a retryable unit failure. Planner and card-Reviewer batches are no longer journaled as `invalid_hypotheses`/`invalid_review` holds; every attempt is cached as `<name>`, `<name>-repair`, `<name>-repair-N`, and a resumed batch replays them without spending budget before resampling.
  - Family discovery retries only invalid model output (previously any exception); initial-Skill shape errors are retried under the same budget; exhaustion is retryable instead of a terminal `DiscoveryError`.
  - The provider loop also retries `ServiceUnavailableError` and `TryAgain`; refused requests raise `ProviderRejected`. JEV transport failures are retried and JEV unit failures are now persisted.
  - The selector no longer converts provider exceptions into `selector_error`; they propagate to the routing unit. Unparsable selector output remains a routing failure.
  - Unit failures are written as `unit-error-v1` records (legacy `error` string kept). Non-retryable failures halt the stage immediately and cancel queued units; a bug halts the whole campaign, including the sibling benchmark job.
  - The campaign decides retries from failure categories, not from exception names or message text.
  - openai `APIError` is classified by HTTP status (408/409/429/5xx retried, other 4xx rejected). Lost environment pipes are `EnvironmentFailure`. A stage killed by a signal is retried; one that exits without its attempt record halts everything.
  - A response-category failure is retryable, but five consecutive response failures of one stage need attention, so a parser defect cannot resample forever.
  - After recording a halting failure, the stage process exits without joining worker threads that are still finishing requests.
  - The L1 checkpoint audit no longer fails permanently because of abandoned requests from an interrupted process (their responses are never used; `tokens_complete` still reports the unknown cost). Previously every interruption during a request made that stage's audit fail for good.
- `runtime/reviewer_retry.py` was removed (`ReviewerOutputError` -> `SchemaViolation`, `ReviewerUpdateError` -> `RepairExhausted`).
- Verification: 270 offline tests pass, including a fault-injection suite (`tests/test_reliability.py`) covering each category at the provider, JEV, selector, discovery, unit-collector, thread-pool and campaign-supervisor boundaries. The L1 (SearchQA, native ALFWorld) and L2 behavior fingerprints are identical to the pre-change tree, so successful paths are unchanged.

## 2026-10-08 no compatibility with earlier runs

The code now reads only the artifact formats it writes. Runs and campaigns created by earlier code must be resumed or audited with that code (for example the `ExpeToSkill-model-roles` checkout); this tree refuses or fails on them.

- Removed: the meta-skill store and `meta_skill_version` fields (the editing strategy is the constant `l2.editor.EDITING_STRATEGY`); the `invalid_*` hold handling; the archived `experiments/` scripts and `tests/test_final_snapshots.py`; the old-campaign detection in `scripts/run_campaign.py` and `check_fresh_campaign.py`; the `long_ver`/`gpt-3.5-turbo-16k` switch of `GPTWrapper`/`LLM_CLS`; `EXPE_LLM_RETRIES` and the manifest's `model`/`timeouts.request_retries` fields (the manifest has a complete `models` role map); the re-exports of error classes from `persistence/io.py` and `runtime/json_output.py`.
- Record formats: unit failure records no longer carry the `error` string; L1, routing and fixed-Skill worker results carry `failure` (`null` on success). L2 repair attempts are cached as `<name>-<n>`. The l2_manifest no longer duplicates `acceptance_mode` outside `config`.
- Required instead of optional: `discovery/card_hashes.json`, the L1 checkpoint `identity`, every frozen L2 config field read by the audit, and family-plan `mode`.
- Card review accepts only `old_outcome`/`new_outcome` and stores the derived `effect` (no `label` alias). The predicted-val reviewer uses one response schema for the request and its protocol hash, so its cache keys changed. The `request_kwargs` fallback for hosts without that argument and the config clone in `SerialEvolutionLoop._reasoning_host` were removed.
- `apply_model_overrides` always sets `agent.llm` to the frozen `l1_executor`.
- Test fixtures changed with these formats. The offline L2 fixture used label-only reviewer responses that reported `improve` on cards whose first attempt already succeeded, which the v6 protocol cannot express; its Skill-aware L1 runs now fail the first autonomous attempt. The campaign `health()` test no longer leaks `EXPE_LLM_BASE_URL` into later tests.
- Verification: 264 offline tests, `ruff check`, the layering test and the native ALFWorld smoke (2 cases, not skipped) all pass. L1 fingerprints (SearchQA, native ALFWorld) are field-identical to the pre-change tree apart from the renamed `error`->`failure` key and the removed `initial_meta_skill_version` field; in the L2 fingerprints every system prompt, instruction and payload structure is unchanged, with differences only in card-derived fields (the fixture's cards differ by design).
