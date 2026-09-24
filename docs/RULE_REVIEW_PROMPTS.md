# Checking existing rules before editing or judging

The L2 prompts now require a current-rule check at three points, without changing the
JSON schema, candidate count, acceptance formula or source-only evidence boundary:

1. Hypothesis generation must read the whole current body, locate existing coverage,
   and preserve conditions, ordering and every AND/OR alternative. The change field
   starts with the relevant literal clause, then describes a remaining supported gap.
2. Body generation rechecks the selected hypothesis. If it is already covered or
   unsupported, it returns `no_change`; it must not substitute an unrelated edit.
   Rules inactive on the current cards are not automatically redundant.
3. Review outputs evidence and a short comparison before its label. The reason states
   the current condition, whether the card supports it as true/false/unknown, the
   respective required actions and the expected success difference. A hypothetical
   executor mistake or saved action alone is not evidence of improvement.

These are model instructions, not a new programmatic proof of their conclusions.
The parser still validates coverage and references; it cannot prove semantic correctness.
New prompts change the implementation fingerprint: start a fresh L2 import rather than
resuming a previous prompt version's L2 directory.

## Targeted development checks (2026-09-23)

The checks used the same `qwen3.6-flash-distill` endpoint/settings as the earlier smoke,
with one generation per case/version and the existing maximum one ID-format correction.
They are prompt-development examples, not held-out benchmark evaluation.

- The original ALFWorld source-card case had proposed deleting “Open the destination
  receptacle if it is closed” for placement on a table. The historical reviewer called
  that an improvement by ignoring the condition.
- A first attempt adding only a stronger checklist still made that reviewer mistake.
  Its planner also produced an overlong malformed response. These failures are retained
  in `runs/prompt-rule-check-20260923/`.
- Requiring a condition/actions/outcome comparison before the label changed the original
  reviewer verdict to `unchanged`. Two synthetic controls also behaved as intended:
  deleting a required open action for a closed box was `regress`, and adding the missing
  action was `improve`. The deletion control needed one correction of its changed-rule
  citation. Results are in `runs/prompt-rule-check-v2-20260923/`.
- The editor rejected the historical “navigate earlier” hypothesis, because the current
  body already navigated before placement. A subsequent planner still misread the
  alternative “or fails”; literal full-clause and AND/OR checks were added.
- In the final editor check, the planner recognized that the existing fallback already
  covered the card, but still emitted it as a hypothesis instead of an empty list.
  Passing that saved output through the actual candidate runner produced `no_change`
  and a `hold: no_distinct_supported_candidate` decision. This remaining planner
  compliance defect is not claimed fixed. Artifacts are in
  `runs/prompt-rule-check-v3-20260923/`.
- All 111 offline regression tests and static checks passed after the final prompt edits.

The targeted failure was blocked in these checks. Reliability across more cards and
skills, and any effect on final success rates, remain unmeasured.
