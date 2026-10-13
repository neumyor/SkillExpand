"""Resolve in task images, assemble on host, validate in fresh task images."""
import argparse
import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

from tencent_task_verifier_cache import CACHE, ROOT, REQUIREMENTS, prepare, verifier_env
from tencent_pipeline_cache import CHUNK_BYTES, write_json
from prepare_pipeline_cache import upload_file_chunks, prepare_python_archive

TASKS = Path('/data2/liyishan/tbench2-openclaw-min/sources/terminal-bench-2-1-modelbest/tasks')
UV = Path('/data2/liyishan/tbench2-openclaw-min/cache/uv/0.9.5/uv-x86_64-unknown-linux-gnu.tar.gz')


async def sandbox(name, directory):
    from harbor.models.task.task import Task
    from harbor.models.trial.paths import TrialPaths
    from tencent_tb2_snapshot import TencentSnapshotE2BEnvironment
    task = Task(task_dir=TASKS / name)
    paths = TrialPaths(trial_dir=directory)
    paths.mkdir()
    env = TencentSnapshotE2BEnvironment(environment_dir=TASKS / name / 'environment',
        environment_name=name, session_id=directory.name, trial_paths=paths,
        task_env_config=task.config.environment)
    try:
        await env.start(False)
        write_json(directory / 'sandbox.json', {'id': env._sandbox.sandbox_id})
    except BaseException:
        if env._sandbox:
            await env.stop(True)
        raise
    return env, task


def uvx_args(name):
    return '-p 3.13 ' + ' '.join('-w ' + shlex.quote(r) for r in REQUIREMENTS[name])


async def resolve(name, folder):
    env, task = await sandbox(name, folder / 'resolution')
    try:
        python_folder, record = prepare_python_archive()
        await upload_file_chunks(env, python_folder / record['archive']['file'], '/tmp/cache-python.tar.gz')
        await env.exec('mkdir -p /tmp/cache-python && tar -xzf /tmp/cache-python.tar.gz --strip-components=1 -C /tmp/cache-python', user='root', timeout_sec=60)
        await env.exec('curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh', user='root', timeout_sec=60)
        await env._sandbox.files.write('/tmp/requirements.in', '\n'.join(REQUIREMENTS[name]), user='root')
        command = '/root/.local/bin/uv pip compile --python /tmp/cache-python/bin/python3.13 /tmp/requirements.in --output-file /tmp/resolved.txt'
        result = await env.exec(command, user='root', timeout_sec=600)
        (folder / 'resolve.log').write_text(result.stdout + result.stderr)
        if result.return_code:
            raise RuntimeError('Same-image dependency resolution failed')
        resolved = await env._sandbox.files.read('/tmp/resolved.txt', user='root')
        (folder / 'requirements.resolved.txt').write_text(resolved)
        arch = await env.exec('uname -m', user='root', timeout_sec=30)
        return task.config.environment.docker_image, arch.stdout.strip(), record
    finally:
        await env.stop(True)


def assemble(name, folder, record):
    from concurrent.futures import ThreadPoolExecutor
    from prefetch_pipeline_wheels import choose, download
    payload = folder / 'payload'
    for sub in ['bin', 'python', 'wheels', 'uv-cache', 'huggingface']:
        (payload / sub).mkdir(parents=True, exist_ok=True)
    tools = CACHE.parent / 'pipeline-build-tools/uv-x86_64-unknown-linux-gnu'
    for binary in ['uv', 'uvx']:
        if not (payload / 'bin' / binary).exists():
            shutil.copy2(tools / binary, payload / 'bin' / binary)
    python_folder, _ = prepare_python_archive()
    managed = payload / 'python' / record['key']
    managed.mkdir(exist_ok=True)
    subprocess.run(['tar', '-xzf', str(python_folder / record['archive']['file']),
                    '--strip-components=1', '-C', str(managed)], check=True)
    resolved = (folder / 'requirements.resolved.txt').read_text()
    pins = [line.split('==', 1) for line in resolved.splitlines()
            if line and not line.startswith((' ', '#')) and '==' in line]
    common = CACHE.parent / 'task-wheelhouse'
    common.mkdir(exist_ok=True)
    def fetch(pin):
        package, version = pin
        for attempt in range(3):
            try:
                url = choose(package, version)
                break
            except (TimeoutError, OSError):
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)
        # Reuse the original pipeline wheelhouse only when the filename matches.
        previous = CACHE.parent / 'pipeline-wheelhouse-v1' / url.rsplit('/', 1)[-1]
        target = common / previous.name
        target_lock = common / (previous.name + '.lock')
        import fcntl
        with target_lock.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if previous.is_file() and not target.exists():
                os.link(previous, target)
            row = download(url, common)
        row.update(package=package, version=version)
        destination = payload / 'wheels' / row['file']
        if not destination.exists():
            os.link(common / row['file'], destination)
        return row
    with ThreadPoolExecutor(max_workers=4) as pool:
        wheels = list(pool.map(fetch, pins))
    if name == 'reshard-c4-data':
        source = os.getenv('TB21_C4_SOURCE_FILE')
        if source:
            import gzip
            source = Path(source)
            if source.stat().st_size != 318185592:
                raise ValueError('Original C4 shard byte length differs')
            with gzip.open(source, 'rb') as stream:
                json.loads(stream.readline())
            hub = payload / 'huggingface/hub/datasets--allenai--c4'
            incomplete = list((hub / 'blobs').glob('*.incomplete'))
            if len(incomplete) != 1:
                raise ValueError('Original Hub download metadata unavailable')
            blob = incomplete[0].name.split('.')[0]
            shutil.copy2(source, hub / 'blobs' / blob)
            snapshot = hub / 'snapshots/1588ec454efa1a09f29cd18ddd04fe05fc8653a2/en/c4-train.00009-of-01024.json.gz'
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            if not snapshot.exists():
                snapshot.symlink_to('../../../blobs/' + blob)
        fixture = '''import importlib
def mirror(url):
    return str(url).replace('https://huggingface.co', 'https://hf-mirror.com')
def patch_client(module):
    original = module.Client.send
    def send(self, request, *args, **kwargs):
        request.url = module.URL(mirror(request.url))
        return original(self, request, *args, **kwargs)
    module.Client.send = send
for name in ('httpx', 'httpx2'):
    try:
        patch_client(importlib.import_module(name))
    except ImportError:
        pass
import requests
original_request = requests.Session.request
def request(self, method, url, *args, **kwargs):
    return original_request(self, method, mirror(url), *args, **kwargs)
requests.Session.request = request
from datasets import load_dataset
d = load_dataset('allenai/c4', data_files={'train':['en/c4-train.00009-of-01024.json.gz']}, split='train')
print('fixture_rows', len(d))
'''
        env = dict(os.environ, UV_PYTHON_INSTALL_DIR=str(payload / 'python'),
                   UV_PYTHON_DOWNLOADS='never', UV_FIND_LINKS=str(payload / 'wheels'),
                   UV_CACHE_DIR=str(payload / 'uv-cache'), UV_OFFLINE='1',
                   HF_HOME=str(payload / 'huggingface'), HF_ENDPOINT='https://hf-mirror.com')
        if os.getenv('TB21_FIXTURE_PROXY'):
            env.update(HTTPS_PROXY=os.environ['TB21_FIXTURE_PROXY'],
                       HTTP_PROXY=os.environ['TB21_FIXTURE_PROXY'], HF_ENDPOINT='https://huggingface.co')
            fixture = "from datasets import load_dataset; d=load_dataset('allenai/c4',data_files={'train':['en/c4-train.00009-of-01024.json.gz']},split='train'); print('fixture_rows',len(d))"
        # Online resolution uses Hub URLs, while the offline builder uses the
        # original data_files argument. Index the same Arrow data under both.
        fixture += '''
from pathlib import Path
from datasets import BuilderConfig
offline_id = BuilderConfig('default').create_config_id(
    config_kwargs={'data_files': {'train': ['en/c4-train.00009-of-01024.json.gz']}},
    custom_features=None)
config_dir = Path(d.cache_files[0]['filename']).parents[2]
alias = config_dir.parent / offline_id
if alias != config_dir and not alias.exists():
    alias.symlink_to(config_dir.name, target_is_directory=True)
print('offline_config', offline_id)
'''
        args = [str(payload / 'bin/uvx'), '-p', '3.13']
        for requirement in REQUIREMENTS[name]:
            args += ['-w', requirement]
        args += ['--from', 'pytest', 'python', '-c', fixture]
        with (folder / 'fixture.log').open('w') as log:
            subprocess.run(args, env=env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=3600)
        # Absolute host tool-environment links must not be exported.
        shutil.rmtree(payload / 'uv-cache')
        (payload / 'uv-cache').mkdir()
    chunks = []
    # Stream locally; E2B only receives the payload during same-image validation.
    process = subprocess.Popen(['tar', '-C', str(payload), '-czf', '-',
                                'bin', 'python', 'wheels', 'uv-cache', 'huggingface'], stdout=subprocess.PIPE)
    try:
        index = 0
        while True:
            block = process.stdout.read(CHUNK_BYTES)
            if not block:
                break
            part = folder / f'part-{index:05d}'
            part.write_bytes(block)
            chunks.append({'name': part.name, 'bytes': len(block)})
            index += 1
        if process.wait():
            raise RuntimeError('Task cache archive failed')
    finally:
        process.stdout.close()
    return wheels, chunks


async def build(name, version):
    folder = CACHE / name / version
    if folder.exists():
        raise ValueError('Version exists; preserve it and use a new version')
    folder.mkdir(parents=True)
    report = {'task': name, 'version': version, 'requirements': REQUIREMENTS[name],
              'status': 'building', 'assembly': 'host_from_same_image_resolution'}
    started = time.monotonic()
    write_json(folder / 'manifest.json', report)
    try:
        image, architecture, record = await resolve(name, folder)
        report.update(image=image, architecture=architecture, python_source=record)
        write_json(folder / 'manifest.json', report)
        wheels, chunks = await asyncio.to_thread(assemble, name, folder, record)
        report.update(status='candidate', wheels=wheels, chunks=chunks,
                      compressed_bytes=sum(p['bytes'] for p in chunks))
    except BaseException as exc:
        report.update(status='failed', error_type=type(exc).__name__, reason=str(exc)[:1000])
        raise
    finally:
        report['build_seconds'] = time.monotonic() - started
        write_json(folder / 'manifest.json', report)
    await validate(name, version)


async def validate(name, version):
    folder = CACHE / name / version
    env, task = await sandbox(name, folder / 'validation' / str(time.time_ns()))
    report = {'purpose': 'offline_dependency_validation_no_agent', 'status': 'failed'}
    try:
        report['cache'] = await prepare(env, version, candidate=True)
        started = time.monotonic()
        result = await env.exec(f'{ROOT}/bin/uvx {uvx_args(name)} pytest --version',
                               user='root', env=verifier_env(), timeout_sec=120)
        report['stdout'] = result.stdout + result.stderr
        if result.return_code:
            raise RuntimeError('Fresh same-image offline dependencies failed')
        if name == 'reshard-c4-data':
            fixture = "from datasets import load_dataset; d=load_dataset('allenai/c4',data_files={'train':['en/c4-train.00009-of-01024.json.gz']},split='train'); print('fixture_rows',len(d))"
            result = await env.exec(f'{ROOT}/bin/uvx {uvx_args(name)} --from pytest python -c {shlex.quote(fixture)}',
                                   user='root', env=dict(verifier_env(), HF_DATASETS_OFFLINE='1', HF_HUB_OFFLINE='1'), timeout_sec=120)
            report['fixture_stdout'] = result.stdout + result.stderr
            if result.return_code:
                raise RuntimeError('Fresh same-image C4 cache lookup failed')
        from harbor.verifier.verifier import Verifier
        env._task_verifier_cache_ready = True
        verifier = Verifier(task=task, trial_paths=env.trial_paths, environment=env)
        outcome = await asyncio.wait_for(verifier.verify(), timeout=task.config.verifier.timeout_sec)
        log = (env.trial_paths.verifier_dir / 'test-stdout.txt').read_text()
        report.update(engineering_reward=outcome.rewards, pytest_started='test session starts' in log,
                      test_outcomes=bool(re.search(r'\b[1-9]\d* (?:passed|failed)\b', log)))
        if not report['pytest_started'] or not report['test_outcomes']:
            raise RuntimeError('Unmodified verifier did not execute tests')
        report.update(status='passed', offline_seconds=time.monotonic() - started)
        manifest = json.loads((folder / 'manifest.json').read_text())
        manifest.update(status='ready', validation=report)
        manifest.pop('error_type', None)
        manifest.pop('reason', None)
        write_json(folder / 'manifest.json', manifest)
    except BaseException as exc:
        report.update(error_type=type(exc).__name__, reason=str(exc)[:1000])
        raise
    finally:
        write_json(folder / 'validation.json', report)
        await env.stop(True)
    print(json.dumps({'task': name, 'version': version, **report}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--credential-pid', type=int, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--validate-only', action='store_true')
    parser.add_argument('--resume-assembly', action='store_true')
    parser.add_argument('--reuse-resolution', type=Path)
    parser.add_argument('tasks', nargs='+', choices=list(REQUIREMENTS))
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', args.version):
        parser.error('Version must be a simple directory name')
    inherited = dict(v.split('=', 1) for v in Path(f'/proc/{args.credential_pid}/environ').read_bytes().decode().split('\0') if '=' in v)
    for key, value in inherited.items():
        if key.startswith(('E2B_', 'HARBOR_E2B_')):
            os.environ[key] = value
    os.environ.update(E2B_DOMAIN='ap-beijing.tencentags.com', HARBOR_E2B_FS_USER='root',
                      TBENCH_PERSIST_SANDBOXES='0', TB21_PIPELINE_CACHE='0', TB21_TASK_VERIFIER_CACHES='{}')
    async def run():
        for name in args.tasks:
            if args.reuse_resolution:
                folder = CACHE / name / args.version
                if folder.exists():
                    raise ValueError('New version required for reused resolution')
                prior = json.loads((args.reuse_resolution / 'manifest.json').read_text())
                if prior['task'] != name or not prior.get('python_source'):
                    raise ValueError('Resolution identity unavailable')
                folder.mkdir(parents=True)
                shutil.copy2(args.reuse_resolution / 'requirements.resolved.txt', folder / 'requirements.resolved.txt')
                prior.update(version=args.version, status='failed', reused_resolution=str(args.reuse_resolution))
                prior.pop('chunks', None)
                write_json(folder / 'manifest.json', prior)
            if args.resume_assembly:
                folder = CACHE / name / args.version
                report = json.loads((folder / 'manifest.json').read_text())
                if report['status'] != 'failed' or not report.get('python_source'):
                    raise ValueError('Only failed unpublished host assemblies can resume')
                write_json(folder / ('manifest.before-resume-' + str(time.time_ns()) + '.json'), report)
                for part in folder.glob('part-*'):
                    raise ValueError('Existing payload parts require a distinct version')
                report['status'] = 'building'
                write_json(folder / 'manifest.json', report)
                try:
                    wheels, chunks = await asyncio.to_thread(assemble, name, folder, report['python_source'])
                    report.update(status='candidate', wheels=wheels, chunks=chunks,
                                  compressed_bytes=sum(p['bytes'] for p in chunks))
                except BaseException as exc:
                    report.update(status='failed', reason=str(exc)[:1000], error_type=type(exc).__name__)
                    raise
                finally:
                    write_json(folder / 'manifest.json', report)
                await validate(name, args.version)
            else:
                await (validate(name, args.version) if args.validate_only else build(name, args.version))
    asyncio.run(run())


if __name__ == '__main__':
    main()
