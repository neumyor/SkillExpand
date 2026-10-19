# E7/E9 Verifier Recovery, 2026-10-14

E7 final panel has 265 accepted unique slots. Missing slots 73-1 and 73-3
are reshard-c4-data. All three requests per slot failed before test execution:
datasets requested default-c33333bd6ac7ada4, while only
default-b04fc8a0b8562884 was available. Their reward.txt zeros are not scores.
The validated repair-20261013-v4 cache already provides the exact original
356318-row fixture with the correct offline alias. The final launcher omitted
TB21_TASK_VERIFIER_CACHES. It now explicitly loads and enables the ready v4
bundle, preserving the pipeline v6 cache and injecting hidden fixtures only
at verification start. A resume preserves all 265 accepted slots and original
requests, with at most three new requests per missing slot per launch.

E9 configure-git-webserver exhausted both physical attempts. In both, the
agent created /root/.ssh/id_ed25519. The unmodified verifier subsequently
calls ssh-keygen for that same path without answering its overwrite prompt.
The host reproducer hangs with an open input pipe and returns immediately
with stdin closed. Raw verifier logs show pytest collected one test, with
no outcome before the sandbox stopped at four hours. The original sandbox
is deleted, so the precise in-sandbox blocked subprocess cannot be inspected;
the overwrite prompt is a demonstrated blocking path, not a recovered stack.

The E9 environment adapter now closes stdin only for /tests/test.sh execution.
It preserves the original verifier script and files, supplies no overwrite
answer, and leaves agent commands unchanged. Two focused tests pass locally
and remotely, including preservation of an existing private key. No formal
E9 rerun, extra budget, contract rewrite, or reward replacement is performed.
This execution compatibility fix is recorded separately from the frozen
contract. Full task-image validation and any new formal E9 attempt remain
outstanding; infra_attempts=2 is already exhausted.

Original remote adapter and launcher files are retained beside each file as
*.before-verifier-fix-20261014. E5 remains stopped.

Resume preflight also exposed legacy Skill serialization drift from the new
optional resources/package_mode fields. Omitting both fields only when they
have their historical empty/false defaults restores the original serialized
identity without editing any library, journal, or cached response. Offline
replay then passes all 23 batches with eight approvals. Package-enabled Skills
retain both fields. This compatibility fix is required before final recovery.

Recovery is now running as PID 1345097. Its live environment contains the
explicit C4 v4 mapping; slots 73-1 and 73-3 each created request 004. Coverage
remains 265/267 pending real verifier outcomes. Global reservation at launch
was 64/250. Earlier launch preflight failures sent no new task requests.
Legacy and package-enabled serialization roundtrip checks pass remotely.
