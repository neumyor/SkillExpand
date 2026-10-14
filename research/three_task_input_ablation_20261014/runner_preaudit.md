# Runner pre-run audit

Passed for the current prepared implementation. This is an offline source and
exact-input audit, not a model-quality result, execution smoke test, or successful
trial claim. No model requests, task sandbox calls, or real slot reservations were
made by this review. At completion there are zero reserved slots and zero model
completion files; the root has not yet been byte-frozen.

Reviewed state.py, generation.py, execution.py, driver.py, evaluator.py,
input_filter.py, monitor.py, remote_start.py, launch.py, freeze.py, protocol.md,
manifest.json, and all six tasks/*/frozen/initial_request-{A,B}.json. Raw historical
inputs were compared programmatically without printing their contents. Hidden
test source, assertion contents and official evaluation stdout were not read.

## Fixed protocol and isolation

- The reservation validator admits only the three declared tasks, one shared
  five-slot sanitized-original baseline per task, two arms, two generation slots
  per arm, three development slots per usable candidate, and five final slots per
  arm. The executor reservation count is capped at 81; the declared generation
  paths provide 12 total slots. A reserved slot cannot execute again, including
  after interruption. Failed/no-change/malformed generations and unresolved
  execution outcomes receive no replacement calls.
- Candidate 2 reads only the same task and arm's candidate-001 package and its
  exact dev/01, dev/02, dev/03 feedback files. If no first package exists, it uses
  the common seed and empty new feedback. Final paths and other arms are never
  discovered or included by this input path.
- Selection re-reads the persisted development records and requires valid status,
  valid sandbox identity and reward=1 for positive evidence. It chooses greatest
  valid successes, ties by earliest candidate. Zero-success usable packages remain
  eligible; no usable package skips final. Selection and the full final package
  are byte-frozen before the five final slots. Final execution does not create
  generation feedback and never influences selection.

## Exact generation inputs

All six initial requests match the frozen generation settings, their corresponding
evidence file, the shared inert seed body and empty development observations. Their
system messages are identical and match the current native build_package_prompt
contract exactly, including EXISTING_RULE_CHECK. The system contains none of the
three task names and adds no task-specific repair mechanism. The actual invocation
is SkillEditor.propose_package; its planned request is persisted before relay
initialization and checked against the actual request.

All six evidence files equal recomputation by the fixed project_history filter.
A contains nine historical rows and two original memory cards for each task.
B contains 8, 2 and 2 numeric reward-zero rows respectively, zero memory cards and
zero result-text contents. Both initial arms use the same sanitized-original
seed with body "Follow the public task requirements." and no resources. This is
the documented joint historical-information removal comparison, not a clean
isolation of leakage alone or new-task generalization.

The feedback path uses only the in-arm new status/reward and projected tool
observations. It omits evaluator errors, exception text, researcher diagnosis and
verifier stdout. The fixed filter checks complete original command arguments and
tool-result text for evaluation markers before head/tail truncation; a match drops
all result text for that step. Development implementations may remain in their
own arm's observations, as declared by the protocol. Raw audit artifacts are not
passed through the generation input path.

## Execution and resource evidence

The existing evaluator launches exactly one official Harbor trial per slot and
checks frozen agent, verifier, task and environment configuration. The native
adapter sets Harbor trial retries to zero. Provider HTTP retries remain part of
the unchanged transport and are distinct from the 81-trial budget. Valid
AgentTimeoutError outcomes backed by official verifier evidence remain valid;
missing reward, identity, configuration or required artifacts remain unresolved.

Global identity registration covers all tasks, arms and phases against historical
exclusions. Duplicate/historical IDs invalidate affected persisted records and
create an integrity_abort marker; verify_freeze then prevents further calls.
Already-running work can finish and be retained, but there is no replacement or
continued acceptance of an independent-run claim. Missing identity alone marks
that slot unresolved. The driver rechecks the abort guard before selection.

Generation setup failures now remain inside the consumed slot's guarded outcome
handling, retain the planned exact request, and allow the next declared slot
without retrying the failed one. Cleanup failure is recorded separately.

Metrics retain observed token counters, rounds, tool-call count, duration and
provider request/response record counts. Resource path references retain command
and observation evidence and are explicitly labeled as references, not proof of
resource execution or correctness. Mounted entrypoint and supplied resource bytes
are checked. Sandbox deletion retains host artifacts; the live final /app
filesystem is not claimed to be retained.

## Scheduling, freezing and validation limits

Nine detached drivers use a conservative 45-worker capacity reservation.
remote_start reads the existing total_worker_limit helper, verifies it matches the
manifest, and holds the existing capacity lock while checking and dispatching.
The parent's live read-only capacity check found 100 existing E10 workers and a
configured limit of 250; the launcher retains that setting, so 145 fits. This
review inspected the recorded evidence and source wiring, not the live process.
The monitor remains the reservation PID while any driver is alive and releases
the reservation when every driver exits. PID/thread-specific temporary filenames
remove the startup status-write collision found in the earlier review.

All ten reviewed Python runner files parse successfully. The parent reports 15
offline tests, including budget, no-repeat reservation, same-arm feedback,
selection ties, generation initialization failure and duplicate identity abort;
the reviewed tests cover those cases. This reviewer did not independently rerun
the full integration tests because the local interpreter lacks langchain.

freeze.py requires both semantic/input and runner passing markers, then binds
runner source, native Python sources, settings, initial requests, protocol,
manifest, audit records, original task definitions/runtime and verifier bytes.
No live run is authorized by a passing marker alone; execution still requires the
completed byte freeze and the user's existing launch authorization. The manifest's
requested Codex model metadata is not proof of the root model switch; its separate
verification field remains explicit.

No unresolved actionable blocker remains in the reviewed implementation.
