# Archived Remote Support Scripts

These scripts preserve the code used on jinan40 on 2026-10-19. They are copied
unchanged from their execution directories, rather than installed entrypoints.

- `diagnose.py` came from
  `/data2/liyishan/SkillExpand-tb21/SkillExpand/diagnostics/e11-package-tail-20261019/`.
  It expects an adjacent `code/` snapshot, frozen source requests at the paths
  in the script, and live environment input on stdin. The three-call results
  are recorded in `../package_tail_diagnostic_20261019.json`.
- `supervise.py` came from
  `/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/tb21-e11-qwen-bare-262144-20261019-package-tail-retry/`.
  The `-02` retry uses the same script. It expects the prepared run's
  `driver.py` next to it and records one generation process without restarting.

The run's driver, prepare, launch, runtime and Harbor scripts match
`research/e11_20261017/`. Its `input_filter.py` matches
`research/library_pilot_20261014/input_filter.py`; runner, study and watcher
match `research/loading_factorial_20261015/`. The environment helper matches
`scripts/e10_bare_environment.py`.

The remote main checkout was at `1f17b22` with unrelated E9, E10 and reader
changes. Its older package editor, schema, package helper and loading-factorial
files do not supersede the published implementations. Original frozen runs and
the dirty main checkout are preserved. Publication does not launch a model
call or formal Qwen evaluation.
