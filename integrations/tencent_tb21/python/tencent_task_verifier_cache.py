"""Task-specific dependency bundles injected only at verification start."""
import asyncio
import json
import os
import re
import shlex
import time
import weakref
from pathlib import Path

from tencent_pipeline_cache import CHUNK_BYTES, write_json

ROOT = '/opt/tb21-verifier-cache'
CACHE = Path('/data2/liyishan/tbench2-openclaw-min/cache/task-verifiers')
REQUIREMENTS = {
    'qemu-startup': ['pytest==8.4.1', 'pytest-json-ctrf==0.3.5'],
    'qemu-alpine-ssh': ['pytest==8.4.1', 'pytest-json-ctrf==0.3.5'],
    'mteb-retrieve': ['pytest==8.4.1', 'mteb==1.36.8', 'pytest-json-ctrf==0.3.5'],
    'torch-tensor-parallelism': ['pytest==8.4.1', 'torch==2.7.0', 'pytest-json-ctrf==0.3.5'],
    'pytorch-model-recovery': ['pytest==8.4.1', 'torch==2.7.1', 'pytest-json-ctrf==0.3.5'],
    'reshard-c4-data': ['pytest==8.4.1', 'datasets==3.6.0', 'tqdm==4.67.1', 'pytest-json-ctrf==0.3.5'],
}
ENVIRONMENTS = weakref.WeakValueDictionary()


def versions():
    value = json.loads(os.getenv('TB21_TASK_VERIFIER_CACHES', '{}'))
    if not isinstance(value, dict) or any(k not in REQUIREMENTS or not isinstance(v, str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', v) for k, v in value.items()):
        raise ValueError('Invalid explicit task verifier cache mapping')
    return value


def register(environment):
    if environment.environment_name in versions():
        ENVIRONMENTS[str(environment.trial_paths.trial_dir.resolve())] = environment


def verifier_env(base=None):
    value = dict(base or {})
    value.update(UV_CACHE_DIR=ROOT + '/uv-cache', UV_PYTHON_INSTALL_DIR=ROOT + '/python',
                 UV_FIND_LINKS=ROOT + '/wheels',
                 UV_PYTHON_DOWNLOADS='never', UV_MANAGED_PYTHON='1', UV_OFFLINE='1',
                 PATH=ROOT + '/bin:/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                 HF_HOME=ROOT + '/huggingface')
    return value


def command_env(environment, command, env):
    if getattr(environment, '_task_verifier_cache_ready', False) and re.match(r'^/tests/test\.sh\s+>', command):
        value = verifier_env(env)
        if environment.environment_name == 'reshard-c4-data':
            value.update(HF_DATASETS_OFFLINE='1', HF_HUB_OFFLINE='1')
            # Agent-authored projects may require build backends beyond the
            # fixed verifier dependencies; resolve them through package mirrors.
            value['UV_OFFLINE'] = '0'
        return value
    return env


async def checked(environment, command, **kwargs):
    result = await environment.exec(command, user='root', **kwargs)
    if result.return_code:
        raise RuntimeError('Verifier dependency preparation failed: ' + (result.stderr or result.stdout)[-1500:])
    return result


def load(task, version, candidate=False):
    if task not in REQUIREMENTS or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', version):
        raise ValueError('Invalid task cache identity')
    folder = CACHE / task / version
    manifest = json.loads((folder / 'manifest.json').read_text())
    if manifest['status'] not in (['candidate', 'ready'] if candidate else ['ready']):
        raise ValueError('Task cache has not passed same-image offline validation')
    if manifest['task'] != task or manifest['version'] != version or manifest['requirements'] != REQUIREMENTS[task]:
        raise ValueError('Task cache dependencies differ')
    if not manifest.get('chunks'):
        raise ValueError('Task cache payload absent')
    for i, part in enumerate(manifest['chunks']):
        if part['name'] != f'part-{i:05d}' or not 0 < part['bytes'] <= CHUNK_BYTES:
            raise ValueError('Invalid task cache layout')
        if (folder / part['name']).stat().st_size != part['bytes']:
            raise ValueError('Task cache part absent or incomplete')
    return folder, manifest


async def prepare(environment, version, candidate=False):
    start = time.monotonic()
    report = {'task': environment.environment_name, 'version': version, 'status': 'preparing'}
    path = environment.trial_paths.trial_dir / 'task_verifier_cache.json'
    try:
        folder, manifest = load(environment.environment_name, version, candidate)
        if manifest['image'] != environment.task_env_config.docker_image:
            raise ValueError('Task cache image differs')
        async with asyncio.timeout(1800):
            arch = (await checked(environment, 'uname -m', timeout_sec=30)).stdout.strip()
            if arch != manifest['architecture']:
                raise ValueError('Task cache architecture differs')
            marker = await environment.exec('cat ' + ROOT + '/ready.json', user='root', timeout_sec=30)
            if marker.return_code == 0 and json.loads(marker.stdout) == {'task': environment.environment_name, 'version': version}:
                report['reused'] = True
            else:
                stage = ROOT + '.staging'
                await checked(environment, f'test ! -e {ROOT} && mkdir -p {stage}/parts {stage}/payload', timeout_sec=30)
                gate = asyncio.Semaphore(4)
                async def upload(part):
                    async with gate:
                        for attempt in range(3):
                            try:
                                await environment._sandbox.files.write(stage + '/parts/' + part['name'],
                                    (folder / part['name']).read_bytes(), user='root', request_timeout=60)
                                return
                            except Exception:
                                if attempt == 2:
                                    raise
                                await asyncio.sleep(attempt + 1)
                await asyncio.gather(*(upload(part) for part in manifest['chunks']))
                unpack = f'cat {stage}/parts/part-* | tar -xzf - -C {stage}/payload'
                await checked(environment, 'bash -o pipefail -c ' + shlex.quote(unpack), timeout_sec=600)
                await environment._sandbox.files.write(stage + '/payload/ready.json',
                    json.dumps({'task': environment.environment_name, 'version': version}), user='root')
                await checked(environment, f'mv {stage}/payload {ROOT} && rm -rf {stage}', timeout_sec=30)
                report['reused'] = False
        environment._task_verifier_cache_ready = True
        report['status'] = 'ready'
    except BaseException as exc:
        environment._task_verifier_cache_ready = False
        report.update(status='failed', error_type=type(exc).__name__)
        raise
    finally:
        report['prepare_seconds'] = time.monotonic() - start
        write_json(path, report)
    return report


async def on_verification_started(event):
    version = versions().get(event.task_name)
    if version:
        key = str((Path(event.config.trials_dir) / event.trial_id).resolve())
        environment = ENVIRONMENTS.get(key)
        if environment is None:
            raise RuntimeError('Task verifier cache environment unregistered')
        await prepare(environment, version)
