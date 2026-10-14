# Terminal artifact policy

New Tencent trials force `record_terminal_session=False` and
`record_terminal_output=False`. Structured `trajectory.json`, verifier results,
skills and reader metadata remain available. Existing worker processes retain
their imported code until they exit.

Install the Harbor extension using the evaluation environment's Python:

```sh
/data2/liyishan/tbench2-openclaw-min/.venv/bin/python scripts/patch_harbor_terminal_logs.py --adapter /data2/liyishan/tb21-tencent-skill/python
```

Run this again after reinstalling Harbor. The installer validates both source
files before writing, rejects unsupported versions, and is idempotent. It adds
the `record_terminal_output` constructor option and optional tmux logging path.
Turning logging off does not disable tmux `capture-pane` observations.

Historical cleanup uses `scripts/cleanup_terminal_logs.py` as root with explicit
`--root` paths, an exclusive `--manifest` audit path and optional `--apply`.
Only regular `.pane` and `.cast` files under a completed trial are eligible.
Incomplete or invalid results, symlinks and open files are skipped. The manifest
is flushed before deletion; candidates are checked again before unlinking.

`scripts/verify_terminal_log_policy.py` checks runtime defaults and tmux startup.
Its `--snapshot` / `--compare` options verify reader reference availability.
`scripts/smoke_terminal_log_policy.py` runs an isolated fix-git trial using
existing service credentials without printing them.
