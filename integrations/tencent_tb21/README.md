# Pipeline verifier dependency cache

Only `torch-pipeline-parallelism` is supported. The live integration is
`/data2/liyishan/tb21-tencent-skill/python`. This directory contains the deployed
source for the optional Harbor runtime integration. It is deployed separately
from the SkillExpand Python package and requires the host's existing Harbor,
E2B and Tencent agent adapter environment (Python 3.12).

## Deployment and recovery

Copy `python/*.py` into `/data2/liyishan/tb21-tencent-skill/python/` on jinan40.
The existing `run_tencent_tb21_smoke.sh` runner imports these modules; keep its
existing Harbor and agent modules installed. Updating these source files does
not reload modules in running processes. Do not restart valid/live requests.

SkillExpand recovery entrypoints live in `scripts/`: timeout reconciliation,
`apply_tb21_task_timeout_zero.py`, and `resume_tb21_policy_tail.py`. The latter
supports `--pipeline-cache-version` and `--wait-for-controls <control> ...`.
The waiting controller reserves zero workers, checks predecessor PID start
identities and holds an exclusive lock through the resulting cached recovery.
It reconciles scores and dispatches only finished, still-missing original
pipeline slots, recording cumulative counts and at most three new requests
per slot. Worker reservations remain capped at 250. Credentials come from
the authorized process environment and optional stdin, never command arguments.

For new sandboxes, the package helper uses DNS `183.60.83.19` and
`https://mirrors.tencentyun.com`, retaining original resolvers as fallback.
Set `TB21_PACKAGE_DNS` to another address or an empty value to opt out. The
configured network is recorded at `/opt/tb21-package-network.json`.
The uv-only bootstrap cache is separate from the full verifier cache switch.
Neither mode changes task tests, dependency requirements or score acceptance.

## Behavior

The default is disabled. In `run_tencent_job.py`, an enabled cache registers a
`VERIFICATION_START` hook. The environment registers its instance using the
absolute trial directory, avoiding sandbox lookup by stale process IDs. The hook
runs after the agent and before Harbor starts the existing verifier timer.
Failure raises `PipelineCachePreparationError`; it cannot produce a reward.

Payloads reside at `/data2/liyishan/tbench2-openclaw-min/cache/pipeline/<version>`.
A manifest lists image, architecture, uv/Python versions, resolved package
versions, complete wheel sources, compressed/expanded size and ordered 8 MiB chunks. Only `ready` versions
are accepted by formal jobs; `candidate` versions are for the standalone validation
command. Versions are immutable after publication and selected explicitly.

The hook uploads one chunk at a time, with an initial attempt plus two retries.
It extracts into an independent staging directory and only publishes a complete
sandbox cache. The entire preparation has a 1,200-second ceiling. A repeated
hook on the same ready version skips transfer. The payload has no task files,
tests, agent work, or verifier results.

Only the original `/tests/test.sh > ...` invocation receives the cache paths.
The curl shim handles exactly the uv 0.9.5 installer URL; other URLs delegate to
system curl. The benchmark script is unchanged. The existing Tencent package
index remains available on cache misses. uv's package download messages are
collected from the normal verifier stdout into `pipeline_cache_downloads.json`.
This does not change timeout cleanup or failure-log collection.

## Build and validation (jinan40)

Use the existing authorized E2B credentials in the process environment, with
`HARBOR_E2B_FS_USER=root`, `E2B_VALIDATE_API_KEY=false` and the actual configured
E2B domain. The optional `--credential-pid` reads only E2B environment settings
from an existing authorized process, without printing or saving keys.

```sh
export PYTHONPATH=/data2/liyishan/tb21-tencent-skill/python:/data2/liyishan/tbench2-openclaw-min/framework/python
/data2/liyishan/tbench2-openclaw-min/.venv/bin/python \
  /data2/liyishan/tb21-tencent-skill/python/prepare_pipeline_cache.py \
  build --version pipeline-uv095-py3139-v6 \
  --wheelhouse /data2/liyishan/tbench2-openclaw-min/cache/pipeline-wheelhouse-v1
/data2/liyishan/tbench2-openclaw-min/.venv/bin/python \
  /data2/liyishan/tb21-tencent-skill/python/prepare_pipeline_cache.py \
  validate --version pipeline-uv095-py3139-v6
```

Build creates one disposable same-image sandbox and prewarms the original uvx
dependency arguments with `pytest --version`. Validation creates another sandbox,
checks offline pytest and torch/transformers imports, compares every installed
package version, tests repeated preparation, and executes the original test.sh
without any agent/model calls. Its reward belongs only to this engineering
smoke, never to the experiment's scored slots. Successful preparation and validation sandboxes are deleted after use. An
unpublished failed preparation sandbox is retained for bounded recovery; its ID
and build logs remain in the version directory. `build --resume-build` refuses
published versions or an unfinished preparation command and reuses completed
wheel uploads. Only successful offline/score-script validation, reduced dependency setup time,
and combined dependency-plus-test time below the unchanged verifier budget publish
`ready`. Transfer/unpack time and end-to-end comparisons are reported separately;
cache availability does not imply an end-to-end speedup.

To opt in **future** explicitly authorized requests after validation:

```sh
export TB21_PIPELINE_CACHE=1
export TB21_PIPELINE_CACHE_VERSION=pipeline-uv095-py3139-v6
```

Do not alter live processes or launch extra experiments as part of cache setup.
Unset `TB21_PIPELINE_CACHE`, or set it to `0`, to retain the ordinary execution
path. The score policy, task script, dependency requirements, verifier timeout
and model/request budgets are unchanged.

## Evidence

Host artifacts: `manifest.json`, `build.log`, `validation.json`, and independent
`build/` and `validation/` trial directories. Each hooked trial writes
`pipeline_cache.json` with separate transfer, unpack and total preparation times.
A cache hit does not erase transfer costs or imply all verifier timeouts are fixed.

Focused tests:

```sh
python3 -m unittest discover -s integrations/tencent_tb21/tests -v
```

### Bounded wheel prefetch

For slow long-lived mirror downloads, `prefetch_pipeline_wheels.py` fetches the
same pinned torch/CUDA/Triton wheels from the Tencent index using eight concurrent
8 MiB HTTP ranges. It checks range boundaries and byte counts and publishes a
wheel only after all ranges finish. The torch 2.7.0 package metadata supplies
exact conditional dependency versions. No dependency versions are substituted.

```sh
/data2/liyishan/tbench2-openclaw-min/.venv/bin/python \
  /data2/liyishan/tb21-tencent-skill/python/prefetch_pipeline_wheels.py \
  --destination /data2/liyishan/tbench2-openclaw-min/cache/pipeline-wheelhouse-v1
```

Pass that directory as `build --wheelhouse <directory>`. The builder uploads those completed large wheels in serial 8 MiB chunks. It then
records the resolved package set and fetches the remaining small wheels at exactly
those versions inside the preparation sandbox. Completed wheel uploads are reused
when resuming an unpublished build. All installation and resolution happens in the
same-image preparation sandbox. Verifier-only
`UV_FIND_LINKS=/opt/tb21-pipeline-cache/wheels` preserves the local wheel source
across sandboxes, with the Tencent index as fallback. The complete wheelhouse is the portable uv package cache. After prewarming and
recording all versions, the builder clears unpacked uv cache copies before export,
so the bundle does not contain both compressed wheels and their extracted duplicates.
The restored `UV_CACHE_DIR` starts empty; uv rebuilds its environment from the local
wheels offline. The Python runtime and uv installer are preserved. Read the actual compressed/expanded sizes in the manifest;
no performance claim is based on that initial estimate. The report separately records host prefetch, same-image sandbox preparation, export,
cache transfer/unpack, offline dependency reconstruction, and pytest timings.

A validation can also use `--cold-reference <path>` with the saved, explicitly
incomplete cold-download observation. Its elapsed time is a lower bound, not a
completed cold benchmark. The report preserves this distinction.

The builder resolves the Python archive URL using the cached uv 0.9.5 binary
(`uv python list 3.13.9 --only-downloads --show-urls --output-format json`).
The exact official archive and its source record are cached in
`/data2/liyishan/tbench2-openclaw-min/cache/pipeline-python-v1`. It is extracted
into uv's managed-runtime directory inside the same-image sandbox, and runtime
selection is checked offline before building the wheel cache. No guessed Python
mirror or different Python build is used.

All resolved transitive versions are also written to verifier-only `UV_CONSTRAINT`.
The original test script is untouched, but fallback downloads must match the frozen
versions. Verifier-only managed-Python selection prevents selecting a different
3.13 patch runtime from the agent environment.
