# SkillExpand source reconstruction

The repository was reconstructed from commit `38f5c530c1361fc79e3e87adb56d14ecc304e610` on 2026-09-23. The published main branch starts with a clean source snapshot. The original repository history and pre-migration source were backed up outside the repository before reconstruction. ExpeL attribution and the Apache 2.0 license remain in NOTICE and LICENSE.

## Code boundaries

All importable code now lives under `src/skillexpand/`. L1, L2, evaluation, runtime, persistence and benchmark environments have separate packages. `pyproject.toml` declares the installable package, dependencies, packaged defaults and `skillexpand` entry point. Both `python -m skillexpand` and the installed command use the same CLI.

The inherited executor remains under `runtime/` to preserve behavior. SearchQA's local context environment and shared QA prompts were extracted from their former HotpotQA locations. FEVER/WebShop environments, unused benchmark configs/data, upstream images, and the retired online L3 updater/pool were removed. Historical record schemas and score-reading primitives remain for existing artifacts.

ALFWorld is an optional dependency; SearchQA imports and executes without the simulator installed. ALFWorld prompt demonstrations, execution budgets and repeat-action behavior are preserved. The existing empty-container observation fix is retained.

## Data and historical experiments

Datasets and run artifacts are no longer tracked. The data preparation script downloads the same ALFWorld assets from a pinned upstream commit, independently of this repository's history. It also accepts an existing archive, records its SHA-256, reuses identical files and refuses to overwrite conflicting assets.

Local task manifests, cold starts and experiment outputs remain in place. The data script prepares upstream assets; it does not reconstruct custom train/admission/final assignments. Pass the original task file when reproducing an experiment.

Completed cold starts retain byte-identical configs and cards. Their old `skill_evolution.l1.adapters:*` references are resolved at the adapter-loading boundary without rewriting saved inputs. Python imports and CLI invocations should use `skillexpand` going forward. An already-started L2/final run has a different implementation fingerprint: import its original cold start into a new run directory instead of mixing implementations under `--resume`.

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
