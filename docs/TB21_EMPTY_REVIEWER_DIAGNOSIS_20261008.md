# E3 empty reviewer diagnosis

Formal evidence: recovery20/recovery21 base tasks 20, 71 and 81 each produced
four empty assembled strings (initial + three corrections). Calls lasted about
337–415 seconds. The official correction budgets remain exhausted. Diagnostic
responses below are not imported into scores, gates or caches.

## Confirmed client behavior

LangChain's installed `ChatOpenAI._generate` concatenates only
`choices[0].delta.content`, ignores `reasoning_content`, and returns no stream
finish reason or usage in this version. Relay's streamed handler replaced null
content with empty strings but did not reject a completed stream without visible
content. This permits reasoning-only streams to become empty successful strings.
Offline replay of 17432 captured reasoning events through the installed client
assembled zero characters; no extra provider call was made for replay.

## Live provider evidence

Diagnostic directories under `/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/`:

- `tb21-empty-reviewer-diagnostic-20261008`: exact task 20 initial prompt,
  model deepseek-v4-flash-0731-tencent, temperature 0, max_tokens 32768,
  no response_format, no stop sequence. With thinking=true it produced 105768
  reasoning characters and 502 visible characters in 291.32s, finish=stop,
  25648 reasoning tokens; valid probability 0.18. With thinking=false it still
  produced 17815 reasoning tokens, then 561 visible characters in 198.60s,
  finish=stop; valid probability 0.72. Thus the switch does not suppress
  reasoning on this endpoint, and visible scoring can vary across the threshold.
- `tb21-empty-reviewer-task71-correction-diagnostic-20261008`: exact last formal
  correction prompt, same true-thinking parameters. After 12612 SSE events in
  148.92s the stream ended without [DONE], final content or finish reason.
  Classified provider_connectivity, not empty successful review.

Reasoning in both tasks repeatedly tries to solve the underlying DNA-primer or
regex/FEN problem. The reviewer prompt explicitly permits reasoning "for as
long as needed" and places the task before review instructions in one user
message. The captured behavior establishes prolonged task-solving rather than
short review reasoning; prompt structure is a plausible contributor, not a
proven isolated cause.

No current probe proves that all historical empty answers exhausted 32768
tokens. Their original raw SSE/finish reasons were not retained. The successful
task 20 repeat demonstrates intermittent completion within the same limit.
The task 71 repeat separately establishes incomplete provider streaming.

## Local repair and boundaries

The streamed handler now counts visible/reasoning characters and finish reasons.
Zero visible output produces an explicit provider_empty_content, or
provider_output_truncated when finish=length, instead of an empty successful
message. Reasoning is not converted into a reviewer score. Focused relay tests:
15 passed, including reasoning-only length termination.

This production repair remains local; existing E1/E5 processes are unchanged.
The diagnostic launcher saves raw SSE, request parameters, visible content,
reasoning and token/finish summaries separately. No token-limit increase,
thinking change or reviewer-prompt change is applied to the formal E3 protocol.
Revised review prompts or fresh formal attempts require a separately recorded
protocol/budget decision; silent cache reuse across those changes is forbidden.

## User-authorized provider-default output retry

The user subsequently requested thinking on, no max length field, streaming,
and another attempt. Independent directory:
`runs/tb21-e3-20261008-authorized-default-output-retry`, PID 1890315.
Exactly one extra request for each missing base task 20, 71 and 81 uses its
last formal correction prompt, thinking=true, stream=true, temperature=0,
and omits both max_tokens and response_format. Provider defaults may still
impose an output limit; this is not a guarantee of unlimited generation.
Request inactivity timeout is 1800 seconds. Raw SSE and channel/usage/finish
metadata are saved per task. Original exhausted budgets remain unchanged.
No result is automatically imported into formal caches or gate scores.

The authorized retry completed: task 71 returned 473 visible characters with
finish=stop in 495.71s (parsed probability 0.03). Task 20 ended after 153.61s,
with 54904 reasoning characters and no visible answer; task 81 ended after
152.41s, with 56286 reasoning characters and no visible answer. Both lacked
finish_reason and [DONE]. The relay logged command completion as status=200;
that log alone does not establish a complete provider SSE response.
The failed task 20 SSE ID was `76645049-e659-47b5-9ded-fdeaf680e9e8`;
failed task 81 SSE ID was `0de980a3-42bb-4229-998b-7c59ac044304`.
Both final captured events were valid reasoning deltas, with no error frame.

## Transport boundary probe

`scripts/diagnose_sse_transport.py` repeats the exact saved task 20 request in
an independent sandbox, preserving the provider response inside the sandbox
before forwarding each line to stdout. It records response headers, HTTP EOF
or exception, command exit status/stderr, and compares the provider file with
host stdout byte for byte. It sends no max_tokens or response_format, uses
thinking=true and stream=true, and consumes no formal correction budget.
Evidence directory: `runs/tb21-sse-transport-diagnostic-20261008-task20`.
Provider request IDs are retained for tracing the gateway/backend boundary;
those internal logs are not available to this probe.

Completed transport probes (same exact prompts as the earlier failed requests):

| Task | Elapsed seconds | Raw bytes on each side | Visible chars | Reasoning chars | End |
| --- | ---: | ---: | ---: | ---: | --- |
| 20 | 707.94 | 14409315 | 503 | 262339 | stop + DONE |
| 81 | 258.46 | 5199742 | 520 | 96917 | stop + DONE |
| 81 repeat | 717.21 | 15443416 | 367 | 285269 | stop + DONE |

All three returned HTTP 200 with chunked transfer encoding, normal HTTP EOF and
command exit 0. Sandbox provider files and host stdout captures are identical,
with no SSE error frames. Task 20 provider ID is
`c14187b5-7867-4aa4-bbcc-5893251fb1cd`, gateway trace
`8019591-1791459801567-3586`. Task 81 provider ID is
`16b0951e-cec3-413f-a7b2-a51d895c7cde`, gateway trace
`v-8019621-1791460000462-26272`.
Task 81 repeat provider ID is `cfa388fb-f959-4d2c-8bbf-c5b7d02fe8cf`,
gateway trace `v-8019608-1791460309038-32224`.

These successful captures establish intact E2B forwarding for these requests
and intermittent completion; they do not prove where the prior failed streams
were cut. A failed capture on both sides is needed to attribute an incomplete
response to the provider HTTP boundary. Even then, gateway versus backend
requires internal logs. A separate task 81 repeat uses directory
`runs/tb21-sse-transport-diagnostic-20261008-task81-repeat`.
That repeat also completed without error; the failure was not reproduced in
these three captures. All diagnostic sandboxes were deleted after preserving
their raw responses and command evidence. No result entered a formal cache.

Task 20 usage reported 65485 output tokens, including 65372 reasoning tokens
and only 113 answer tokens. Its prolonged reasoning is directly observed, but
its normal stop does not explain the prior missing end marker. Omitting the
request limit does not establish that provider defaults are unlimited.

## Current conclusion

Long reasoning, intermittent completion and misleading command-level
status=200 logging are confirmed. Historical streams lacking [DONE] remain
invalid infrastructure outcomes, never zero-reward reviews. E2B forwarding was
byte-complete on all three new requests, but successful requests cannot rule
out an intermittent forwarding failure in the old runs. The precise failed
boundary remains unresolved. Do not label the old issue a fixed 150s timeout,
output-limit failure, gateway failure or backend failure without further
evidence. The retained failed SSE IDs above are the next lookup keys for
provider-side logs; a future failed dual-capture can distinguish provider EOF
from E2B output loss. Existing experiment gates and reviewer budgets remain
unchanged.
