"""Build and validate pipeline caches in disposable same-image E2B sandboxes."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import shlex
import time
import re
import inspect
import subprocess
import tarfile
from pathlib import Path

from tencent_pipeline_cache import (CACHE_ROOT, CHUNK_BYTES, ROOT, TASK, UVX_ARGS,
    REQUIRED, checked_exec, prepare, verifier_env, write_json)

TASK_ROOT = Path('/data2/liyishan/tbench2-openclaw-min/sources/terminal-bench-2-1-modelbest/tasks') / TASK
UV_ARCHIVE = Path('/data2/liyishan/tbench2-openclaw-min/cache/uv/0.9.5/uv-x86_64-unknown-linux-gnu.tar.gz')


def prepare_python_archive():
    from prefetch_pipeline_wheels import download
    tools = CACHE_ROOT.parent/'pipeline-build-tools'
    binary = tools/'uv-x86_64-unknown-linux-gnu/uv'
    if not binary.exists():
        tools.mkdir(parents=True,exist_ok=True)
        with tarfile.open(UV_ARCHIVE) as archive: archive.extractall(tools,filter='data')
    rows = json.loads(subprocess.check_output([str(binary),'python','list','3.13.9','--only-downloads','--show-urls','--output-format','json']))
    records = [r for r in rows if r['key']=='cpython-3.13.9-linux-x86_64-gnu']
    if len(records)!=1: raise RuntimeError('Cannot resolve the exact approved Python runtime')
    record = records[0]
    folder = CACHE_ROOT.parent/'pipeline-python-v1'
    folder.mkdir(exist_ok=True)
    previous = folder/'python.json'
    if previous.exists():
        saved = json.loads(previous.read_text())
        artifact = folder/saved['archive']['file']
        if saved['url'] == record['url'] and saved['key'] == record['key'] and artifact.is_file() and artifact.stat().st_size == saved['archive']['bytes']:
            return folder,saved
    record['archive'] = download(record['url'],folder)
    write_json(folder/'python.json',record)
    return folder,record


async def sandbox(trial_dir, resume=False):
    from harbor.models.task.task import Task
    from harbor.models.trial.paths import TrialPaths
    from tencent_tb2_snapshot import TencentSnapshotE2BEnvironment
    from tencent_package_mirrors import configure
    task = Task(task_dir=TASK_ROOT)
    paths = TrialPaths(trial_dir=trial_dir)
    paths.mkdir()
    environment = TencentSnapshotE2BEnvironment(environment_dir=TASK_ROOT/'environment',
        environment_name=TASK, session_id=trial_dir.name, trial_paths=paths,
        task_env_config=task.config.environment)
    if resume:
        from e2b import AsyncSandbox
        previous=json.loads((trial_dir/'sandbox.json').read_text())
        environment._sandbox=await AsyncSandbox.connect(previous['id'],timeout=14400)
        running=await environment.exec("pgrep -x 'uv|uvx|python3.13|tar|gzip|split'",user='root',timeout_sec=30)
        if running.return_code == 0:
            raise RuntimeError('Previous preparation command is still running; do not start a duplicate')
    else:
        await environment.start(False)
    write_json(trial_dir / 'sandbox.json', {'id': environment._sandbox.sandbox_id})
    certificate = Path('/etc/ssl/certs/ca-certificates.crt')
    await environment._sandbox.files.write(str(certificate), certificate.read_bytes(), user='root')
    await configure(environment._sandbox, 'https://mirrors.tencent.com')
    return environment, task


async def upload_file_chunks(environment, source, target):
    existing=await environment.exec('stat -c %s ' + shlex.quote(target),user='root',timeout_sec=30)
    if existing.return_code==0 and existing.stdout.strip()==str(source.stat().st_size):
        return
    with source.open('rb') as stream:
        index = 0
        while block := stream.read(CHUNK_BYTES):
            await environment._sandbox.files.write(f'{target}.{index:05d}', block, user='root', request_timeout=60)
            index += 1
    await checked_exec(environment, f'cat {target}.[0-9]* > {target}.tmp && mv {target}.tmp {target} && rm {target}.[0-9]*', timeout_sec=60)


async def packages(environment):
    script = "import importlib.metadata as m,json,platform,sys; print(json.dumps({'packages':{d.metadata['Name'].lower().replace('_','-'):d.version for d in m.distributions()},'python_version':platform.python_version(),'python_build':platform.python_build(),'executable':sys.executable}))"
    result = await checked_exec(environment, f'{ROOT}/bin/uvx {UVX_ARGS} --from pytest python -c {shlex.quote(script)}', env=verifier_env({'UV_OFFLINE':'1'}), timeout_sec=120)
    return json.loads(result.stdout.strip())


async def fetch_wheels_in_sandbox(environment, wheels):
    from prefetch_pipeline_wheels import download
    worker = ("from concurrent.futures import ThreadPoolExecutor\nfrom pathlib import Path\n"
              "from urllib.request import Request,urlopen\nimport json,time,sys\nBLOCK=8*1024*1024\n"
              + inspect.getsource(download) +
              "\nfolder=Path(sys.argv[2]); folder.mkdir(parents=True,exist_ok=True)\n"
              "for item in json.loads(Path(sys.argv[1]).read_text()):\n"
              " print(json.dumps(download(item['url'],folder)),flush=True)\n")
    await environment._sandbox.files.write('/tmp/pipeline-range-fetch.py', worker, user='root')
    await environment._sandbox.files.write('/tmp/pipeline-wheel-sources.json', json.dumps(wheels), user='root')
    command = f'"$({ROOT}/bin/uv python find 3.13.9)" /tmp/pipeline-range-fetch.py /tmp/pipeline-wheel-sources.json {ROOT}/wheels >> /tmp/pipeline-wheel-fetch.log 2>&1'
    result = await environment.exec(command, user='root', env=verifier_env({'SSL_CERT_FILE':'/etc/ssl/certs/ca-certificates.crt'}), timeout_sec=3600)
    log = await environment._sandbox.files.read('/tmp/pipeline-wheel-fetch.log', user='root')
    (environment.trial_paths.trial_dir/'wheel-fetch.log').write_text(log)
    if result.return_code:
        raise RuntimeError('Wheel prefetch failed: ' + log[-2000:])


async def build(args):
    destination = CACHE_ROOT / args.version
    if destination.exists() and not args.resume_build:
        raise RuntimeError('Cache version already exists; use --resume-build for an unpublished build')
    if args.resume_build and json.loads((destination/'manifest.json').read_text()).get('status') in ('candidate','ready'):
        raise RuntimeError('Published candidates cannot be rebuilt in place')
    destination.mkdir(parents=True,exist_ok=True)
    environment = None
    started = time.monotonic()
    report = {'version': args.version, 'status': 'building', 'task': TASK}
    write_json(destination / 'manifest.json', report)
    try:
        python_folder, python_record = await asyncio.to_thread(prepare_python_archive)
        environment, task = await sandbox(destination/'build',resume=args.resume_build)
        await checked_exec(environment, f'mkdir -p {ROOT}/bin {ROOT}/shim {ROOT}/python {ROOT}/uv-cache {ROOT}/wheels', timeout_sec=30)
        await upload_file_chunks(environment, UV_ARCHIVE, ROOT + '/uv-0.9.5.tar.gz')
        await checked_exec(environment, f'tar -xzf {ROOT}/uv-0.9.5.tar.gz -C {ROOT} && cp {ROOT}/uv-x86_64-unknown-linux-gnu/uv* {ROOT}/bin/', timeout_sec=60)
        runtime = await checked_exec(environment, f'{ROOT}/bin/uv --version', timeout_sec=30)
        if not runtime.stdout.strip().startswith('uv 0.9.5'):
            raise RuntimeError('Expected the approved uv 0.9.5 binary')
        bootstrap = time.monotonic()
        await upload_file_chunks(environment, python_folder/python_record['archive']['file'], '/tmp/pipeline-python.tar.gz')
        managed = ROOT + '/python/' + python_record['key']
        await checked_exec(environment, f'mkdir -p {managed} && tar -xzf /tmp/pipeline-python.tar.gz --strip-components=1 -C {managed} && rm /tmp/pipeline-python.tar.gz', timeout_sec=120)
        await checked_exec(environment, f'{ROOT}/bin/uv python find 3.13.9', env=verifier_env({'UV_OFFLINE':'1'}), timeout_sec=30)
        report['python_source'] = python_record

        if args.wheelhouse:
            report['wheelhouse_prefetch'] = json.loads((args.wheelhouse/'prefetch.json').read_text())
            upload_started=time.monotonic()
            for wheel in sorted(args.wheelhouse.glob('*.whl')):
                await upload_file_chunks(environment,wheel,ROOT+'/wheels/'+wheel.name)
                print('staged wheel: '+wheel.name,flush=True)
            report['wheelhouse_upload_seconds']=time.monotonic()-upload_started
        cmd = f'{ROOT}/bin/uv python install 3.13.9 && {ROOT}/bin/uvx {UVX_ARGS} pytest --version'
        # Save output as it happens in the independent preparation sandbox.
        result = await environment.exec(f'({cmd}) > /tmp/pipeline-build.log 2>&1', user='root', env=verifier_env(), timeout_sec=3600)
        log = await environment._sandbox.files.read('/tmp/pipeline-build.log', user='root')
        (destination/'build.log').write_text(log)
        if result.return_code:
            raise RuntimeError('Cache prewarm failed; see build.log')
        resolved = await packages(environment)
        if resolved['python_version'] != '3.13.9' or any(resolved['packages'].get(k) != v for k,v in REQUIRED.items()):
            raise RuntimeError('Resolved dependency versions differ from the approved versions')
        from prefetch_pipeline_wheels import choose
        existing = {x['package'] for x in report.get('wheelhouse_prefetch',{}).get('wheels',[])}
        missing = []
        for name, version in resolved['packages'].items():
            if name not in existing:
                url = await asyncio.to_thread(choose, name, version)
                missing.append({'package': name, 'version': version, 'url': url})
        await fetch_wheels_in_sandbox(environment, missing)
        report['additional_wheel_sources'] = missing
        # The complete wheelhouse is the portable uv package cache. Do not also
        # archive its unpacked copies and absolute tool-environment links.
        await checked_exec(environment, f'{ROOT}/bin/uv cache clean && mkdir -p {ROOT}/uv-cache', env=verifier_env(), timeout_sec=120)
        report['cache_layout'] = 'complete-wheelhouse'
        report.update(resolved, uv_version='0.9.5', image=task.config.environment.docker_image,
            architecture=(await checked_exec(environment, 'uname -m')).stdout.strip(),
            dependency_prepare_seconds=time.monotonic()-bootstrap,
            package_index='https://mirrors.tencent.com/pypi/simple',
            sources={'uv':'https://github.com/astral-sh/uv/releases/download/0.9.5/uv-x86_64-unknown-linux-gnu.tar.gz', 'python':'uv 0.9.5 managed CPython 3.13.9 download'},
            root=ROOT)
        # No task files or test outputs; only the private dependency tree.
        export_started = time.monotonic()
        command = f'mkdir -p /tmp/pipeline-export && tar -C {ROOT} -czf - bin shim python uv-cache wheels uv-0.9.5.tar.gz | split -b {CHUNK_BYTES} -d -a 5 - /tmp/pipeline-export/part-'
        await checked_exec(environment, 'bash -o pipefail -c ' + shlex.quote(command), timeout_sec=600)
        listing = await checked_exec(environment, "stat -c '%n %s' /tmp/pipeline-export/part-*", timeout_sec=30)
        chunks=[]
        for line in listing.stdout.splitlines():
            remote,size=line.rsplit(' ',1)
            name=Path(remote).name
            data=await environment._sandbox.files.read(remote, format='bytes', user='root', request_timeout=60)
            if len(data)!=int(size): raise RuntimeError('Incomplete export chunk')
            temporary=destination/(name+'.tmp');temporary.write_bytes(data);temporary.replace(destination/name)
            chunks.append({'name':name,'bytes':len(data)})
        report.update(chunks=chunks, compressed_bytes=sum(x['bytes'] for x in chunks),
            expanded_bytes=int((await checked_exec(environment, f'du -sb {ROOT}')).stdout.split()[0]),
            export_seconds=time.monotonic()-export_started,
            build_seconds=time.monotonic()-started,status='candidate')
        print(json.dumps({k:report[k] for k in ('status','version','compressed_bytes','expanded_bytes','build_seconds')}),flush=True)
    except BaseException as exc:
        report.update(status='failed',error_type=type(exc).__name__)
        raise
    finally:
        write_json(destination/'manifest.json',report)
        if environment:
            for name in ('pipeline-build.log','pipeline-wheel-fetch.log'):
                try:
                    content=await environment._sandbox.files.read('/tmp/'+name,user='root',request_timeout=20)
                    (destination/'build'/name).write_text(content)
                except Exception:
                    pass
            if report.get('status')=='candidate':
                await environment.stop(True)
            else:
                print('Unpublished preparation sandbox retained for --resume-build',flush=True)


async def validate(args):
    destination=CACHE_ROOT/args.version
    report={'status':'validating', 'purpose':'dependency_cache_validation_no_agent'}
    environment=None
    try:
        trial_dir=destination/'validation'/str(time.time_ns())
        report['trial_dir']=str(trial_dir)
        environment,task=await sandbox(trial_dir)
        report['cache']=await prepare(environment,args.version,allow_candidate=True)
        started=time.monotonic()
        result=await checked_exec(environment,f'{ROOT}/bin/uvx {UVX_ARGS} pytest --version',env=verifier_env({'UV_OFFLINE':'1'}),timeout_sec=120)
        report['offline_pytest']=result.stdout.strip()
        await checked_exec(environment,f"{ROOT}/bin/uvx {UVX_ARGS} --from pytest python -c 'import torch,transformers; print(torch.__version__,transformers.__version__)'",env=verifier_env({'UV_OFFLINE':'1'}),timeout_sec=120)
        actual=await packages(environment)
        manifest=json.loads((destination/'manifest.json').read_text())
        if actual['packages']!=manifest['packages'] or actual['python_version']!='3.13.9':
            raise RuntimeError('New sandbox dependency versions do not match')
        report['offline_seconds']=time.monotonic()-started
        report['repeat_prepare']=await prepare(environment,args.version,allow_candidate=True)
        from harbor.verifier.verifier import Verifier
        started=time.monotonic()
        verifier=Verifier(task=task,trial_paths=environment.trial_paths,environment=environment)
        result=await asyncio.wait_for(verifier.verify(),timeout=task.config.verifier.timeout_sec)
        report.update(verifier_seconds=time.monotonic()-started,reward=result.rewards)
        log=(environment.trial_paths.verifier_dir/'test-stdout.txt').read_text()
        report['installer_hit']='pipeline cache: uv 0.9.5 installer hit' in log
        report['entered_pytest']='test session starts' in log
        report['download_lines']=[l for l in log.splitlines() if l.startswith('Downloading ')]
        if not report['installer_hit'] or not report['entered_pytest'] or report['download_lines']:
            raise RuntimeError('Original verifier script cache validation failed')
        report['status']='passed'
        # Readiness requires demonstrable cold-preparation savings, not a promise.
        report['cold_prepare_seconds'] = manifest['dependency_prepare_seconds'] + manifest.get('wheelhouse_prefetch',{}).get('seconds',0)
        report['measured_dependency_improvement']=report['offline_seconds'] < report['cold_prepare_seconds']
        report['end_to_end_improvement']=report['cache']['prepare_seconds']+report['offline_seconds'] < report['cold_prepare_seconds']
        if args.cold_reference:
            reference = json.loads(args.cold_reference.read_text())
            if reference.get('kind') != 'incomplete_cold_download' or reference.get('complete') is not False:
                raise RuntimeError('Expected an explicitly incomplete cold-download reference')
            report['cold_reference'] = reference
            report['faster_than_cold_lower_bound'] = report['cache']['prepare_seconds']+report['offline_seconds'] < reference['dependency_elapsed_seconds']
            report['measured_dependency_improvement'] = report['offline_seconds'] < reference['dependency_elapsed_seconds']
        report['within_verifier_budget'] = report['offline_seconds'] + report['verifier_seconds'] < task.config.verifier.timeout_sec
        if report['measured_dependency_improvement'] and report['within_verifier_budget']:
            manifest['status']='ready'
            manifest['validation']=report
            write_json(destination/'manifest.json',manifest)
        else:
            report['status']='compatible_not_ready'
        print(json.dumps(report),flush=True)
    except BaseException as exc:
        report.update(status='failed', error_type=type(exc).__name__)
        raise
    finally:
        write_json(destination/'validation.json',report)
        if environment: await environment.stop(True)


def main():
    p=argparse.ArgumentParser();p.add_argument('operation',choices=['build','validate']);p.add_argument('--version',required=True);p.add_argument('--credential-pid',type=int);p.add_argument('--wheelhouse',type=Path);p.add_argument('--cold-reference',type=Path);p.add_argument('--resume-build',action='store_true')
    args=p.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]*', args.version):
        p.error('version must be a simple directory name')
    if args.credential_pid:
        raw=Path(f'/proc/{args.credential_pid}/environ').read_bytes()
        values=dict(x.decode().split('=',1) for x in raw.split(b'\0') if b'=' in x)
        os.environ.update({k:v for k,v in values.items() if k.startswith('E2B_') or k.startswith('HARBOR_E2B_')})
    os.environ.update(E2B_VALIDATE_API_KEY='false',HARBOR_E2B_FS_USER='root',TBENCH_PERSIST_SANDBOXES='0')
    asyncio.run(build(args) if args.operation=='build' else validate(args))

if __name__=='__main__':main()
