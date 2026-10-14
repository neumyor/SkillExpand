# Three-task historical-input ablation

This source snapshot implements [protocol.md](protocol.md): three tasks, a shared
five-trial sanitized baseline, two historical-information arms, at most two
candidate slots per arm, three development trials per usable candidate and five
final trials for the selected candidate. The maximum is 81 executor trials and
12 generation slots. Reserved calls are never repeated after interruption.

| Files | Responsibility |
| --- | --- |
| `prepare_sources.py`, `configure.py`, `input_filter.py` | Collect the declared historical inputs, sanitize the shared seed and construct A/B evidence |
| `freeze.py`, `state.py` | Bind audited inputs/source bytes and reserve the fixed slots |
| `generation.py` | Call the native package editor once per generation slot and retain exact requests/results |
| `evaluator.py`, `execution.py`, `driver.py` | Run frozen Harbor trials, track global sandbox identities and choose by development successes with earliest-slot ties |
| `launch.py`, `remote_start.py`, `monitor.py`, `live_status.py` | Dispatch on the recorded cluster, reserve shared capacity and observe progress |
| `completion_audit.py` | Independently audit a delivered archive using native prompt/proposal replay, official outcomes and full resource bytes |
| `resource_analysis.py` | Inspect saved resource reads, copies, invocation evidence and failure observations |
| `report.py` | Render the original runner's result summary |

The original run directory is
`/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/three-task-input-ablation-20261014`.
At publication the run is incomplete: disk exhaustion caused interrupted writers
and some empty artifacts. The offline auditor understands documented
infrastructure recovery markers, retains lost-source limitations and gives no
official score to reconstructed unresolved records. The actual recovery helper
and completion of the live run are not part of this publication.

To audit a separately delivered archive without executing tasks:

```bash
python research/three_task_input_ablation_20261014/completion_audit.py \
  --root /absolute/path/to/complete-run-archive
```

The archive must contain its matching `code/skillexpand`,
`code/summarize_tb21_empirical.py`, `frozen/`, `tasks/`, `bindings.json` and binding
snapshots. The auditor uses those saved sources and settings rather than the
current development checkout. `resource_analysis.py --help` lists the offline
resource-analysis options.

The source-only regression tests use synthetic inputs. They cover filtering,
slot budgets, interrupted calls, candidate feedback isolation, selection, native
replay, official result checks and resource evidence. See the command in
[the research index](../README.md).

The preparation pipeline also requires historical E1/E4/E6 runs, the frozen
executor runtime and the native source snapshot at `255a67c`. `configure.py`
expects these dependencies to have been staged before input/runner preaudit and
`freeze.py`; source checkout alone is not a ready-to-launch experiment. The
preaudit documents here record the original review, not a new approval marker.
