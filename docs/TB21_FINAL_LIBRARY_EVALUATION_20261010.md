# Final Library Evaluations

Each source must finish its existing L2 round, report zero invalid batches,
pass its 267-slot execution audit and pass read-only round journal replay.
Freeze all final heads, including unchanged v0 Skills. Do not select only
accepted v1 Skills or resume the cancelled bootstrap E4 evaluation.

| Stage flag | Aligned source | Selector | Executor |
| --- | --- | --- | --- |
| E4 | E3 | DEEPSEEK_up5zdj | qwen3.6-flash-distill |
| E3_FINAL | E3 | DEEPSEEK_up5zdj | DEEPSEEK_up5zdj |
| E5_FINAL | E5 | qwen3.6-flash-distill | qwen3.6-flash-distill |
| E6_FINAL | E6 | qwen3.6-flash-distill | qwen3.6-flash-distill |

Use `scripts/run_tb21_empirical.py --stage <flag> --source <aligned-run>
--run-dir <independent-evaluation-run> --workers 16`. `--prepare-only` makes
no model/sandbox calls, but still requires a completed, audited source.
Optional `--selector-model` and `--executor-model` must match the table;
resume rejects role or frozen library changes. Existing E1 and historical
bootstrap E4 formats retain their prior paths.

Independent directory names under `runs/`:

- `tb21-e4-e3-final-library-empirical-20261010`
- `tb21-e3-final-library-empirical-20261010`
- `tb21-e5-final-library-empirical-20261010`
- `tb21-e6-final-library-empirical-20261010`

Every panel has 89 tasks and three independent attempts. Three canaries count
toward its 267 scores. The library, tasks, source audit and model roles are
persisted before execution. A capacity lock reserves the workers before any
relay starts; all runs share `TB21_TOTAL_WORKER_LIMIT=250`. Start eligible
panels in the listed order when capacity permits. New source rounds or
additional v0 control panels are not authorized by this workflow.

Provide the method credential in `OPENAI_API_KEY` (or `TB21_METHOD_API_KEY`)
and Qwen credential in `TB21_EXECUTOR_API_KEY`, inherited only from authorized
live process environments. The selector relay captures its own role key before
Harbor inherits the executor key. Never persist credentials in config or logs.
Use internal package DNS/mirrors and explicit pipeline v6 cache as described
in `../integrations/tencent_tb21/README.md`.

On continuation, choose the earliest real verifier-valid request per slot
after checking executor, task, trajectory, selection/catalog/version and mounted
Skill body. Retain prior record and request evidence. An unfinished latest
request blocks retry; otherwise at most three requests are added per slot
per launch. Missing rewards remain unresolved and never become fixed zero.

Reports include pass@1, pass@3, per-task initial/final outcomes and descriptive
deltas. Same task, model roles and execution settings are required to mark a
comparison paired/comparable. In particular E5_FINAL changes the v0 DeepSeek
selector to Qwen, so its delta cannot isolate Skill evolution. Disclose the
internal mirror/cache change and do not claim independent generalization or
statistical significance. No empirical result is available until the frozen
panel actually completes and its audit passes.

The existing hourly `tb2-1` automation now follows the three L2 stages and
four frozen panels; stop it only once all seven are complete and audited.
