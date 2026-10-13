"""Opt-in, verifier-only dependency cache for the pipeline benchmark."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import time
import weakref
from pathlib import Path

TASK = 'torch-pipeline-parallelism'
ROOT = '/opt/tb21-pipeline-cache'
CACHE_ROOT = Path('/data2/liyishan/tbench2-openclaw-min/cache/pipeline')
CHUNK_BYTES = 8 * 1024 * 1024
PREPARE_SECONDS = 1200
UVX_ARGS = '-p 3.13 -w pytest==8.4.1 -w torch==2.7.0 -w transformers==4.55.0 -w pytest-json-ctrf==0.3.5'
REQUIRED = {'pytest': '8.4.1', 'torch': '2.7.0', 'transformers': '4.55.0', 'pytest-json-ctrf': '0.3.5'}
_ENVIRONMENTS = weakref.WeakValueDictionary()


class PipelineCachePreparationError(RuntimeError):
    pass


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n')
    tmp.replace(path)


def enabled():
    value = os.getenv('TB21_PIPELINE_CACHE', '0')
    if value not in ('0', '1'):
        raise ValueError('TB21_PIPELINE_CACHE must be 0 or 1')
    return value == '1'


def register_environment(environment):
    if environment.environment_name == TASK and enabled():
        _ENVIRONMENTS[str(environment.trial_paths.trial_dir.resolve())] = environment


def load_bundle(version, cache_root=CACHE_ROOT, allow_candidate=False):
    if not version or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]*', version):
        raise PipelineCachePreparationError('An explicit valid cache version is required')
    folder = Path(cache_root) / version
    manifest = json.loads((folder / 'manifest.json').read_text())
    allowed = ('candidate', 'ready') if allow_candidate else ('ready',)
    if manifest.get('status') not in allowed or manifest.get('version') != version:
        raise PipelineCachePreparationError('Cache version has not passed validation')
    if manifest.get('task') != TASK or manifest.get('uv_version') != '0.9.5' or manifest.get('python_version') != '3.13.9':
        raise PipelineCachePreparationError('Cache runtime identity mismatch')
    for package, expected in REQUIRED.items():
        if manifest.get('packages', {}).get(package) != expected:
            raise PipelineCachePreparationError(f'Cache dependency mismatch: {package}')
    chunks = manifest.get('chunks', [])
    if not chunks:
        raise PipelineCachePreparationError('Cache has no payload')
    for index, entry in enumerate(chunks):
        if entry['name'] != f'part-{index:05d}' or not 0 < entry['bytes'] <= CHUNK_BYTES:
            raise PipelineCachePreparationError('Invalid cache chunk layout')
        if (folder / entry['name']).stat().st_size != entry['bytes']:
            raise PipelineCachePreparationError(f'Missing or incomplete cache chunk: {entry["name"]}')
    return folder, manifest


async def checked_exec(environment, command, **kwargs):
    result = await environment.exec(command, user='root', **kwargs)
    if result.return_code:
        raise PipelineCachePreparationError((result.stderr or result.stdout or '')[-2000:])
    return result


def verifier_env(base=None, constrained=False):
    env = dict(base or {})
    env.update(UV_CACHE_DIR=ROOT + '/uv-cache', UV_PYTHON_INSTALL_DIR=ROOT + '/python',
               UV_FIND_LINKS=ROOT + '/wheels',
               PATH=ROOT + '/shim:' + ROOT + '/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
               UV_NO_PROGRESS='1')
    if constrained:
        env.update(UV_CONSTRAINT=ROOT + '/constraints.txt', UV_MANAGED_PYTHON='1', UV_PYTHON_DOWNLOADS='never')
    return env


def verifier_command_env(environment, command, env):
    # Harbor's unmodified verifier invocation; never affect agent/setup execs.
    if getattr(environment, '_pipeline_cache_ready', False) and re.match(r'^/tests/test\.sh\s+>', command):
        return verifier_env(env, constrained=True)
    return env


# Exact installer URL only. Other curl requests retain ordinary behavior.
CURL_SHIM = '''#!/bin/sh
for arg in "$@"; do
  if [ "$arg" = https://astral.sh/uv/0.9.5/install.sh ]; then
    cat <<'INSTALL'
set -eu
printf '%s\\n' 'pipeline cache: uv 0.9.5 installer hit' >&2
mkdir -p "$HOME/.local/bin"
tar -xzf /opt/tb21-pipeline-cache/uv-0.9.5.tar.gz -C /opt/tb21-pipeline-cache
cp /opt/tb21-pipeline-cache/uv-x86_64-unknown-linux-gnu/uv /opt/tb21-pipeline-cache/uv-x86_64-unknown-linux-gnu/uvx "$HOME/.local/bin/"
printf 'export PATH="%s/.local/bin:$PATH"\\n' "$HOME" > "$HOME/.local/bin/env"
INSTALL
    exit 0
  fi
done
exec /usr/bin/curl "$@"
'''


async def install_bundle(environment, folder, manifest):
    if environment.task_env_config.docker_image != manifest['image']:
        raise PipelineCachePreparationError('Cache and task image differ')
    arch = (await checked_exec(environment, 'uname -m', timeout_sec=30)).stdout.strip()
    if arch != manifest['architecture']:
        raise PipelineCachePreparationError('Cache and sandbox architecture differ')
    marker = ROOT + '/ready.json'
    previous = await environment.exec('cat ' + marker, user='root', timeout_sec=30)
    if previous.return_code == 0 and json.loads(previous.stdout).get('version') == manifest['version']:
        environment._pipeline_cache_ready = True
        return {'reused': True, 'transfer_seconds': 0, 'unpack_seconds': 0, 'bytes': 0}
    # A private staging tree; no shared writable cache between sandboxes.
    staging = ROOT + '.staging'
    await checked_exec(environment, f'rm -rf {staging} && mkdir -p {staging}/parts {staging}/payload', timeout_sec=30)
    start = time.monotonic()
    for entry in manifest['chunks']:
        payload = (folder / entry['name']).read_bytes()
        for attempt in range(3):
            try:
                await environment._sandbox.files.write(staging + '/parts/' + entry['name'], payload, user='root', request_timeout=60)
                break
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(attempt + 1)
    transferred = time.monotonic()
    # Stream the ordered chunks into tar, avoiding an additional complete archive.
    unpack = (f'for part in {staging}/parts/part-*; do cat "$part" || exit; rm "$part"; done '
              f'| tar -xzf - -C {staging}/payload')
    await checked_exec(environment, 'bash -o pipefail -c ' + shlex.quote(unpack), timeout_sec=600)
    await environment._sandbox.files.write(staging + '/payload/shim/curl', CURL_SHIM, user='root')
    constraints = ''.join(f'{name}=={version}\n' for name, version in sorted(manifest['packages'].items()))
    await environment._sandbox.files.write(staging + '/payload/constraints.txt', constraints, user='root')
    runtime = await checked_exec(environment, f'chmod +x {staging}/payload/shim/curl && {staging}/payload/bin/uv --version', timeout_sec=30)
    if not runtime.stdout.strip().startswith('uv 0.9.5'):
        raise PipelineCachePreparationError('Cached uv binary has the wrong version')
    await environment._sandbox.files.write(staging + '/payload/ready.json', json.dumps({'version': manifest['version']}), user='root')
    await checked_exec(environment, f'test ! -e {ROOT} && mv {staging}/payload {ROOT} && rmdir {staging}/parts {staging}', timeout_sec=30)
    environment._pipeline_cache_ready = True
    return {'reused': False, 'transfer_seconds': transferred - start,
            'unpack_seconds': time.monotonic() - transferred,
            'bytes': sum(c['bytes'] for c in manifest['chunks'])}


async def prepare(environment, version, cache_root=CACHE_ROOT, allow_candidate=False):
    report_path = environment.trial_paths.trial_dir / 'pipeline_cache.json'
    start = time.monotonic()
    report = {'version': version, 'status': 'preparing', 'task': TASK}
    write_json(report_path, report)
    try:
        folder, manifest = load_bundle(version, cache_root, allow_candidate)
        async with asyncio.timeout(PREPARE_SECONDS):
            report.update(await install_bundle(environment, folder, manifest))
        report['status'] = 'ready'
    except BaseException as exc:
        environment._pipeline_cache_ready = False
        report.update(status='failed', error_type=type(exc).__name__)
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise PipelineCachePreparationError(f'Pipeline cache preparation failed: {type(exc).__name__}') from exc
    finally:
        report['prepare_seconds'] = time.monotonic() - start
        write_json(report_path, report)
    return report


async def on_verification_started(event):
    if event.task_name != TASK or not enabled():
        return
    key = str((Path(event.config.trials_dir) / event.trial_id).resolve())
    environment = _ENVIRONMENTS.get(key)
    if environment is None:
        raise PipelineCachePreparationError('Pipeline cache environment was not registered')
    await prepare(environment, os.getenv('TB21_PIPELINE_CACHE_VERSION', ''))


async def on_trial_ended(event):
    if event.task_name != TASK or not enabled():
        return
    trial = Path(event.config.trials_dir) / event.trial_id
    log = trial / 'verifier/test-stdout.txt'
    try:
        # Ordinary uv output names distributions that actually download.
        available = log.exists()
        downloads = sorted(set(re.findall(r'^Downloading (.+)$', log.read_text(errors='replace'), re.M))) if available else None
        write_json(trial / 'pipeline_cache_downloads.json',
                   {'stdout_available': available, 'downloaded_packages': downloads})
    except OSError:
        logging.getLogger(__name__).exception('Could not save cache download telemetry')
