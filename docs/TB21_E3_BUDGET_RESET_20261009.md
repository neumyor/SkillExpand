# E3 Authorized Correction Budget Reset

User authorization: E3 reset correction budgets (2026-10-09).

- Run: `runs/tb21-e3-20261009-up5zdj-authorized-budget-reset`.
- Source: `runs/tb21-e3-20261009-model-DEEPSEEK_up5zdj-r2`.
- Model: `DEEPSEEK_up5zdj`; provider and E2B credentials loaded from the updated desktop environment without persisting secrets.
- Worker count: 1; 174 existing reviewer cache records reused.
- Tasks 9, 60, 73: one initial request plus at most three format corrections per missing reviewer identity. Historical correction counts remain recorded.
- Other tasks retain existing correction rules. Valid cache records are not rerun.
- Previous process stopped and its relay sandbox cleaned before relaunch.
- Tencent E2B relay, template defaults enabled, task persistence 0.
- Production completion still requires both batches, journal and audit; candidate mean must exceed base mean before commit.
- This is closed-set predicted acceptance, not independent verifier evaluation.

The user clarified E5 second-batch continuation, documented in `TB21_E5_SECOND_BATCH_20261009.md`.

## Output Limit Continuation

The user subsequently requested E3 `max_tokens=65536`. New run:
`runs/tb21-e3-20261009-up5zdj-max65536`, source the budget-reset run above.
177 valid cache records are reused, including recovered tasks 9, 60 and 73.
The old process and relay sandbox were stopped before launch; its pending task
81 request is recorded as a configuration restart in `max_tokens_transition.json`,
without manufacturing a score or resetting correction budgets. The new run sends
65536 for E3 requests, preserves thinking and streaming, and retains one worker.
The user subsequently requested E5 65536 as well, and DeepSeek requests now
default to 65536. See `TB21_E5_SECOND_BATCH_20261009.md`.
