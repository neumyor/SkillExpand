# Reproduce the generic-module restart

Use `../SkillExpand/.venv/bin/python` locally and the existing authorized desktop
credential loader. Keys travel through SSH stdin and never appear in argv or
saved configuration. The remote dependency environment remains `.venv2`.

The independent remote root is
`/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/generic-skill-evolution-20261014`.
`code/skillexpand` is the copied native module. The task and Tencent runtime are
frozen evaluator-only copies of the original experiment. No old targeted candidate,
successful implementation, checker or feedback is imported into generation.

`frozen/input.json` preserves the original nine rows, existing memory, original
Skill and public contract byte for byte. `frozen/projected_input.json` uses the
pre-fixed tool-results projection in `evaluate.py`; the same extractor supplies
new development observations. Missing and truncated evidence is explicit.

Commands from this directory:

```bash
../SkillExpand/.venv/bin/python launch.py generate --candidate 1
../SkillExpand/.venv/bin/python launch.py dev --candidate 1 --batch 1
../SkillExpand/.venv/bin/python freeze_audit.py --candidate 1
../SkillExpand/.venv/bin/python launch.py accept --candidate 1 --batch 1
../SkillExpand/.venv/bin/python report.py
```

The local freeze step requires complete synced generation artifacts and the
independent semantic review. Sync its marker/snapshots and audited modules to the
remote root before acceptance. A missing or changed binding blocks execution.

Generation attempts are numbered and retain precise request, SSE, completion,
usage and errors. Candidate files are model outputs; never edit them manually.
If a candidate fails, the next candidate may consume only that new candidate's
mechanically recorded development observations. Use a new acceptance batch number;
successes from different batches cannot be combined. Five fresh environments run
in parallel with unchanged Qwen parameters and official verifier.

The scoped native module change is in `generic_editor.diff`; related tests are
`SkillExpand/tests/test_package_editor.py`. Focused L2 tests passed 59/59 with
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`. Existing unrelated checkout changes remain.

`report.py` independently checks saved official outcomes, full mounted resources,
configuration, sandbox identity, binding snapshots and complete batch summary.
It reads official completion markers using the existing validator without
disclosing hidden output or feeding it to evolution. Unknown usage remains
unknown; provider monetary prices are unavailable. Single-task acceptance does
not establish cross-task transfer or a statistical reliability guarantee.
