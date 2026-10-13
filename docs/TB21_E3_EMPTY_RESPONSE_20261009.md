# E3 Empty Reviewer Response Diagnosis

## Confirmed Findings

The active remote relay lacks the visible-content guard already present in the
local relay implementation. The installed LangChain streaming client assembles
only `choices[0].delta.content`; it ignores `reasoning_content`, finish reasons
and usage. A complete reasoning-only stream therefore becomes an empty string
and is recorded as a successful request by the old remote path.

The actual E3 task 9 request was inspected inside its live relay sandbox:
`model=DEEPSEEK_up5zdj`, `max_tokens=32768`, `stream=true`, temperature 0,
`enable_thinking=true`. Neither `stop` nor `response_format` was sent.
Two production responses had zero assembled characters after 395.28 and
391.43 seconds. Their original SSE was not retained, so their finish reason
cannot be determined retrospectively.

## Independent Raw SSE Evidence

Both probes use the same task 9 correction prompt through Tencent E2B. Their
outputs are diagnostic only and are not imported into reviewer cache or gates.

- `runs/tb21-e3-empty-task9-20261009-uncapped`: no `max_tokens` field, thinking
  enabled, streaming. Finished after 112.31 seconds with `[DONE]` and
  `finish_reason=length`. Provider reported 8192 completion tokens, all 8192
  reasoning tokens, 30409 reasoning characters, and zero visible characters.
  This proves an effective provider default cap even when the request omits
  `max_tokens`; it does not establish a universal provider limit.
- `runs/tb21-e3-empty-task9-20261009-capped`: `max_tokens=32768`, same model,
  thinking and streaming. Finished after 299.95 seconds with `[DONE]` and
  `finish_reason=stop`, 465 visible characters and a valid reviewer JSON.
  Provider reported 23534 completion tokens, including 23433 reasoning tokens
  and 98623 reasoning characters.

Thus omission of `max_tokens` does not remove output limits. Reasoning can
consume the full budget before a final reviewer JSON is emitted. The old relay
then hides the distinction between truncation and an ordinary empty response.
This is not explained by a newline stop parameter: none was sent.
The capped result also shows that 32768 does not always cause an empty answer.
Historical empty production calls cannot be conclusively assigned a termination
reason because their raw SSE was not saved.

Task 9 asks for a logic-gate circuit implementing Fibonacci of an integer square
root. The retained reasoning explored detailed gate counts and multiplication
design rather than only estimating executor success. The reviewer instruction
permits reasoning "for as long as needed". This is a plausible contributor to
long reasoning, not a tested causal explanation.

## Validation And Runtime Boundaries

An isolated copy of the local relay and tests passed all 15 relay tests on
jinan40, including reasoning-only length termination becoming
`provider_output_truncated` rather than empty success.

The diagnosis script now accepts an explicit model ID and labels zero-content
diagnostics by their finish reason. Active E1, E3 and E5 processes are left on
their existing runtime code; the relay guard has not been hot-swapped underneath
them. Future deployment must record the runtime code change before resuming.

Empty/truncated responses are not reviewer scores and never become reward 0.
Diagnostic outputs do not consume or reset formal correction budgets.

At the diagnosis checkpoint E3 had obtained valid later responses for task 9
and task 60, increasing its reviewer cache from 174 to 176. No final stage
completion or verifier score is claimed.
