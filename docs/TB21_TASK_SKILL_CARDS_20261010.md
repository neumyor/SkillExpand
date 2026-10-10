# TB2.1 Skill-library experience attribution

## Protocol

The evolution subject is the actually executed Skill library head. External
Harbor slots are grouped by `(original task_id, loaded_skill_key)`, not by an
assumed task-family assignment. Different selectors choosing different Skills
is valid. A group's trials retain source_task_id, source_attempt_index,
source_slot, source_request, result/trajectory paths and each original selection.
Local trial indices remain contiguous. A card's reward is the disjunction of
its own trials; the empirical panel still counts original task/attempt slots.

Round manifests use `format: task-skill-v1` and `external_harbor: true`.
`experience_ids` index the cards, files, executed Skill keys and original slots.
Batch `experience_ids` identify evidence; batch `task_ids` are distinct original
task IDs. Cards from different tasks enter batches of the same actual family.
The library retains Skills with no collected evidence without an update.
Legacy one-card-per-task rounds remain supported.

## Gate and recovery

Source audit checks task/attempt/request identity, real binary verifier reward,
executor model, trajectory, selector catalog/Skill key, activation and mounted
Skill body against the accepted slot record. AgentTimeoutError with a real
reward is allowed; other failed infrastructure results are not accepted.
Each of the 267 original slots must occur exactly once. Card count is variable.

Publication requires full coverage. Previous card files and manifest are kept
under `cards-before-task-skill-v1` and `manifest-before-task-skill-v1.json`.
Migration stops if an old L2 batch plan or journal exists. Re-publication of
the same manifest validates and reuses its cards. Journals reference stable
experience IDs and replay in frozen batch order.

`EvolutionConfig.external_experience_format = task-skill-v1` makes absent or
invalid external manifests/cards fatal rather than starting new L1 execution.
`run_tb21_aligned.py --l2-only` audits and publishes existing accepted evidence
before any worker reservation or model relay. Existing source/model flags and
worker identity must still match the run's alignment; the worker cap is 250.
It preserves the current L2 model roles, correction ledgers, batch size,
candidate count and acceptance settings.

## Offline operation

From `/data2/liyishan/SkillExpand-tb21/SkillExpand`:

```bash
PYTHONPATH=src:scripts .venv2/bin/python scripts/rebuild_tb21_skill_cards.py \
  runs/tb21-e3-e1aligned-free-library-20261009 \
  runs/tb21-e5-e1aligned-free-library-20261009 \
  runs/tb21-e6-e1aligned-qwen-experience-deepseek-evolver-20261009
```

The default performs read-only grouping/validation and prints coverage.
Add `--publish` only for a fully covered run to archive and publish derived
cards and execution_audit.json under the stage lock. No mode executes L1.
After publication passes, resume the stage using its existing alignment flags
with `--l2-only`. Do not restart the full rollout path to progress L2.

## Verified snapshot, 2026-10-10

| Run | Accepted original slots | Grouped cards | Missing slots |
| --- | --- | --- | --- |
| E3 | 265/267 | 121 | 81-1, 81-3 |
| E5 | 266/267 | 113 | 81-1 |
| E6 | 266/267 | 117 | 81-2 |

All accepted slots mapped exactly once to their actual Skill; there were no
source/activation/body mismatches. Multiple selected Skills per task are now
informational. Full publication and L2 remain pending missing real scores.
The active pipeline recovery was not duplicated or restarted.

Focused tests cover grouping 1/3 versus 2, same-Skill aggregation, cross-task
family batches, invalid sources, duplicate/missing slots, idempotent migration,
old-card retention, external input failures without L1, cached batch restore,
frozen journal order and grouped round audit. The new tests and existing
aligned/serial L2 regression tests total 67 passing checks locally.
