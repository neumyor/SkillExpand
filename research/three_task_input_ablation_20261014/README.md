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
| `recover_infrastructure.py` | Preserve damaged bytes and recover consumed-slot wrappers offline after confirming related processes have exited; retain unresolved outcomes where official evidence is incomplete |
| `resume_launch.py`, `resume_start.py`, `resume_monitor.py` | Continue unfinished arms through the unchanged frozen driver, preserve existing reservations and monitor the continuation PIDs |
| `final_report.py` | Render final results only after the independent completion audit reports both passed and protocol complete |

The original run directory is
`/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/three-task-input-ablation-20261014`.
The fixed protocol is complete: 63/81 executor slots and all 12 generation slots
were consumed. The independent remote completion audit passed with 51 valid
trials, 45 official successes and 12 unresolved trials. Disk exhaustion caused
interrupted writers and some empty or truncated artifacts. This publication
includes the offline recovery and continuation helpers, plus the
[final report](final_report.md) and [resource analysis](resource_analysis.md).
The runtime records remain in the complete run archive. Recovery preserves
damaged original bytes and never repeats or replaces consumed executor or
generation slots. It can reconstruct a valid wrapper only from a complete,
independently audited official evidence chain; otherwise the outcome remains
unresolved with no reward credit. Continuation preserves the frozen code,
inputs, model settings and existing slot budgets.

A completed protocol does not imply complete reward, token or timing
measurements. The 12 unresolved rewards remain unknown, including one final
log B runtime failure. All losses, failed/invalid proposals and interrupted
generations remain in the reports; consumed calls were never replaced.
`final_report.py` refuses to render before the completion audit passes and
reports protocol complete.

| Task | Baseline | A final | B final |
| --- | --- | --- | --- |
| cancel-async-tasks | ? 0 ? 0 ? | 1 1 1 1 1 | 1 0 0 0 1 |
| multi-source-data-merger | 1 ? ? 1 ? | 1 1 1 1 1 | 1 1 1 1 1 |
| log-summary-date-ranges | 1 1 1 ? 1 | 1 1 1 1 1 | 1 ? 1 1 1 |

`?` means unresolved, not zero. Both arms used the same sanitized seed.
This joint information ablation cannot isolate leakage or establish blind
new-task generalization. Exact requests, SSE, official outcomes, mounted skills,
input lineage and recovery originals referenced by the reports live in the
separate complete archive; they are not part of this source publication.

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
