"""E10 recovery-only bounded APT setup with durable diagnostic output."""
import asyncio
import json
import shlex
from pathlib import Path

from e10_setup_recovery_environment import E10SetupRecoveryEnvironment, BOOTSTRAP

OPTIONS = '-o Acquire::Retries=0 -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15 -o DPkg::Lock::Timeout=10 -o APT::Update::Error-Mode=any'
SNAPSHOT = 'date -u; ps -eo pid,ppid,pgid,stat,wchan:28,args; echo LOCKS; cat /proc/locks; echo DPKG_AUDIT; dpkg --audit'


def stage_command(label, command):
    root = '/tmp/e10-apt-recovery/' + label
    return (f'mkdir -p {root}; '
        f'(sleep 80; {{ {SNAPSHOT}; }} > {root}/during.txt 2>&1) < /dev/null > /dev/null 2>&1 & watcher=$!; '
        f'timeout -k 3 90 sh -c {shlex.quote(command)} < /dev/null '
        f'> {root}/stdout.log 2> {root}/stderr.log; rc=$?; echo "$rc" > {root}/exit_code; '
        'kill "$watcher" 2>/dev/null || true; wait "$watcher" 2>/dev/null || true; '
        f'{{ {SNAPSHOT}; }} > {root}/after.txt 2>&1; '
        f'cat {root}/stdout.log; cat {root}/stderr.log >&2; exit "$rc"')


class E10AptStagedEnvironment(E10SetupRecoveryEnvironment):
    async def _apt_stage(self, label, command):
        local = self.trial_paths.trial_dir / 'apt_setup' / label
        local.mkdir(parents=True, exist_ok=True)
        record = {'command': command, 'process_deadline_seconds': 90,
                  'transport_deadline_seconds': 115, 'additional_retries': 0}
        try:
            result = await super().exec(stage_command(label, command), user='root', timeout_sec=115)
            record['exit_code'] = result.return_code
            if result.return_code != 0:
                raise RuntimeError(f'E10 APT stage {label} failed: exit {result.return_code}')
            return result
        except BaseException as exc:
            record['error_type'] = type(exc).__name__
            raise
        finally:
            (local / 'stage.json').write_text(json.dumps(record, indent=2) + '\n')
            for name in ['stdout.log', 'stderr.log', 'exit_code', 'during.txt', 'after.txt']:
                try:
                    data = await asyncio.wait_for(self._sandbox.files.read(
                        '/tmp/e10-apt-recovery/' + label + '/' + name,
                        user='root', request_timeout=5), timeout=6)
                    (local / name).write_text(data)
                except Exception as exc:
                    if name != 'during.txt':
                        record.setdefault('capture_errors', {})[name] = type(exc).__name__
            (local / 'stage.json').write_text(json.dumps(record, indent=2) + '\n')

    async def exec(self, command, **kwargs):
        if command != BOOTSTRAP:
            return await super().exec(command, **kwargs)
        probe = await super().exec('command -v python3 >/dev/null 2>&1 && command -v tmux >/dev/null 2>&1',
                                   user='root', timeout_sec=30)
        if probe.return_code == 0:
            return probe
        await self._apt_stage('update', f'apt-get {OPTIONS} update')
        return await self._apt_stage('install', 'packages=""; '
            'command -v python3 >/dev/null 2>&1 || packages="$packages python3-minimal"; '
            'command -v tmux >/dev/null 2>&1 || packages="$packages tmux"; '
            f'[ -z "$packages" ] || DEBIAN_FRONTEND=noninteractive apt-get {OPTIONS} install -y $packages')
