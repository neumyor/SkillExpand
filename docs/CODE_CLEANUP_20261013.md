# Obsolete code cleanup (2026-10-13)

Reviewed against `origin/liyishan` at `5530972`.

## Removed

- Eleven unused helpers in `src/skillexpand/runtime/utils.py`: `shuffled_chunks`,
  `load_trajectories_log`, `split_logs_by_task`, `recompute_stats`,
  `plot_trial_stats`, `alfworld_results_per_env_name`,
  `alfworld_results_per_env_name_log`, `get_webshop_mean_score`,
  `get_webshop_mean_scores`, `get_split_eval_idx_list`, and `mode_results`.
  Repository-wide reference searches found only their definitions and calls
  within this obsolete group. They consume the upstream ExpeL text-log format;
  the plotting code assumes 100 or 134 tasks, and several branches concern
  unsupported WebShop, HotpotQA, and Fever benchmarks. Current evaluation uses
  structured artifacts in `evaluation/`, `persistence/`, and the TB2.1 scripts.
- The packaged HuggingFace embedding defaults and the unconditional factory
  override that replaced them with `lexical`. The YAML now declares the actual
  lexical default directly; factory callers still receive the same backend.

## Retained deliberately

- `Count`, task/environment parsing, exact-match scoring, token budgeting,
  `random_divide_list`, and `save_trajectories_log`: still referenced by executor
  or benchmark code.
- `MetaSkill` and `MetaSkillStore`: the online L3 updater was retired, but the
  current L2 loop, editor, and audit still use the policy schema/store.
- Legacy family IDs, card-review outcomes, and structured-skill conversion:
  compatibility paths still have callers and support persisted artifacts.
- Historical TB2.1 recovery/diagnostic scripts: the code map explicitly retains
  them as recovery and authorization evidence. Their age alone does not establish
  that they are safe to remove.
- Reader code and vendored HKUDS-UpSkill scripts: current functionality, not
  obsolete infrastructure.

This cleanup changes no experiment artifacts and performs no remote restart.
The removed helpers are internal runtime utilities; any external code importing
these undocumented helpers will need to migrate to the current artifact readers.

## Updated regression expectation

The invalid-selector regression still expected an empty `error`, although the
current progressive routing records `RuntimeError: progressive Skill selection
failed: unparsable`. The failure was reproduced against the unmodified baseline.
The test now checks that diagnostic and verifies that no Skill body was loaded;
the runtime behavior is unchanged.

## Validation

Focused executor, embedding, learning, experience, and progressive-routing tests
pass (71 tests). A separate import check confirms all repository imports from
`runtime.utils` resolve, all three benchmark configs build lexical embeddings,
and SearchQA/ALFWorld parsing helpers retain their behavior. No live model or
remote benchmark was run.
