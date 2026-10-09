# E5 Second Batch Continuation

Authorization: user requested E5 to continue its second batch (2026-10-09).

- Source: `runs/tb21-e5-20261008-recovery21-authorized-continuation`.
- Run: `runs/tb21-e5-20261009-up5zdj-second-batch`.
- Reused reviewer cache: 434 records; existing first-batch journal retained.
- Provider model: `DEEPSEEK_up5zdj`; Qwen executor unchanged.
- Updated desktop provider and E2B credentials injected without storing secrets; carriage returns removed.
- Initial continuation: one reviewer worker, thinking enabled, streaming, no `max_tokens`, reviewer `response_format` omitted.
- E5 correction budgets are not reset. Only missing reviewer identities are requested.
- Tencent E2B relay with template-default startup; task persistence 0.
- Old source is read-only; new request identities and transport log are independent.
- Completion requires both production batches plus journal and audit. Candidate mean must exceed base mean before commit.
- These are closed-set predicted acceptance results, not Harbor verifier scores or independent test generalization.

## Output Limit Update

User requested E5 `max_tokens=65536` and a DeepSeek default of 65536.
Current run: `runs/tb21-e5-20261009-up5zdj-max65536`, starting with 450
valid cached reviewer records and the existing first-batch journal. Prior process
and relay were stopped; the pending old-limit identity is recorded in
`max_tokens_transition.json`. Correction budgets were not reset.

The GPT wrapper and Tencent relay now default DeepSeek model names and aliases
(`deepseek-*`, `DEEPSEEK_*`, including provider-prefixed names) to 65536 only when
no limit is specified. Explicit limits take precedence. The continuation driver
sends 65536 for E3 and E5 and records the authorized runtime code changes.
Focused checks: 7 request-policy tests and 14 relay tests passed.
