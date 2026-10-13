"""Same-image infrastructure probes; never execute an agent or score a trial."""
import argparse
import asyncio
import json
import os
import time
from pathlib import Path

TASKS = Path('/data2/liyishan/tbench2-openclaw-min/sources/terminal-bench-2-1-modelbest/tasks')
EVIDENCE = Path('/data2/liyishan/tbench2-openclaw-min/cache/verifier-repair-20261013')


async def probe(name):
    from harbor.models.task.task import Task
    from harbor.models.trial.paths import TrialPaths
    from tencent_tb2_snapshot import TencentSnapshotE2BEnvironment
    from tencent_pipeline_cache import write_json
    task = Task(task_dir=TASKS / name)
    directory = EVIDENCE / name / str(time.time_ns())
    paths = TrialPaths(trial_dir=directory)
    paths.mkdir()
    environment = TencentSnapshotE2BEnvironment(environment_dir=TASKS / name / 'environment',
        environment_name=name, session_id=directory.name, trial_paths=paths,
        task_env_config=task.config.environment)
    report = {'task': name, 'purpose': 'infrastructure_only_no_agent', 'status': 'running',
              'image': task.config.environment.docker_image}
    try:
        await environment.start(False)
        commands = [
            ('dns', 'getent hosts mirrors.tencentyun.com mirrors.tencent.com'),
            ('apt', 'apt-get update && apt-get install -y curl' +
                (' expect' if name == 'qemu-startup' else ' sshpass' if name == 'qemu-alpine-ssh' else '')),
            ('uv', 'curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh; "$HOME/.local/bin/uv" --version'),
        ]
        for label, command in commands:
            result = await environment.exec(command, user='root', timeout_sec=180)
            (directory / (label + '.log')).write_text(result.stdout + result.stderr)
            report[label] = {'return_code': result.return_code}
            if result.return_code:
                raise RuntimeError(label + ' probe failed')
        report['status'] = 'passed'
    except Exception as exc:
        report.update(status='failed', error_type=type(exc).__name__, reason=str(exc)[:1000])
    finally:
        if environment._sandbox:
            await environment.stop(True)
        write_json(directory / 'probe.json', report)
        print(json.dumps({'directory': str(directory), **report}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--credential-pid', type=int, required=True)
    parser.add_argument('tasks', nargs='+')
    args = parser.parse_args()
    inherited = dict(v.split('=', 1) for v in Path(f'/proc/{args.credential_pid}/environ').read_bytes().decode().split('\0') if '=' in v)
    for key in ['E2B_API_KEY', 'E2B_DOMAIN', 'HARBOR_E2B_TEMPLATE', 'HARBOR_E2B_FS_USER']:
        if key in inherited:
            os.environ[key] = inherited[key]
    os.environ.update(E2B_DOMAIN='ap-beijing.tencentags.com', HARBOR_E2B_FS_USER='root',
                      TBENCH_PERSIST_SANDBOXES='0', TB21_PIPELINE_CACHE='0')
    async def run():
        for name in args.tasks:
            await probe(name)
    asyncio.run(run())


if __name__ == '__main__':
    main()
