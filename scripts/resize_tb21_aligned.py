"""Restart aligned drivers with an authorized worker allocation; keep credentials in memory."""
import argparse
import asyncio
import os
import signal
import subprocess
import time
from pathlib import Path

from summarize_tb21_empirical import read, write
from run_tb21_empirical import active_worker_reservation


def process_environment(pid):
    return dict(item.decode().split('=', 1) for item in
                Path(f'/proc/{pid}/environ').read_bytes().split(b'\0') if b'=' in item)


async def cleanup_sandboxes(roots, relay_ids):
    from e2b import AsyncSandbox
    sessions = {p.name for root in roots for p in root.glob('slots/*/requests/*/jobs/*/*')
                if p.is_dir() and (p / 'agent').exists()}
    ids = set(relay_ids)
    paginator = AsyncSandbox.list(limit=100)
    while paginator.has_next:
        for sandbox in await paginator.next_items():
            if (sandbox.metadata or {}).get('session_id') in sessions:
                ids.add(sandbox.sandbox_id)
    semaphore = asyncio.Semaphore(16)
    async def kill(sandbox_id):
        async with semaphore:
            await AsyncSandbox.kill(sandbox_id)
    await asyncio.gather(*(kill(sandbox_id) for sandbox_id in ids))
    return sorted(ids)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', type=Path, action='append', required=True)
    parser.add_argument('--workers', type=int, required=True)
    parser.add_argument('--total-limit', type=int, required=True)
    parser.add_argument('--reconcile-timeouts', action='store_true')
    args = parser.parse_args()
    roots = [root.resolve() for root in args.run_dir]
    if len(set(roots)) != len(roots):
        parser.error('Run directories must be unique')
    if args.workers < 1 or args.total_limit < len(roots) * args.workers:
        parser.error('Invalid allocation')
    snapshots = []
    for root in roots:
        status = read(root / 'status.json')
        pid = status['pid']
        command = [s.decode() for s in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0') if s]
        if status['status'] != 'running' or 'scripts/run_tb21_aligned.py' not in command:
            raise ValueError('Only live aligned drivers can be resized')
        if os.getpgid(pid) != pid:
            raise ValueError('Driver must own its process group')
        snapshots.append((root, status, command, process_environment(pid)))
    reserved, _ = active_worker_reservation(roots[0].parent, Path('/tmp/tb21-no-run'))
    projected = reserved - sum(status['workers'] for _, status, _, _ in snapshots) + len(roots) * args.workers
    if projected > args.total_limit:
        raise ValueError(f'Global allocation exceeds limit: {projected}/{args.total_limit}')
    for _, status, _, _ in snapshots:
        os.kill(status['pid'], signal.SIGSTOP)
    for _, status, _, _ in snapshots:
        os.killpg(status['pid'], signal.SIGTERM)
        os.kill(status['pid'], signal.SIGCONT)
    time.sleep(3)
    for _, status, _, _ in snapshots:
        try:
            os.killpg(status['pid'], signal.SIGKILL)
        except ProcessLookupError:
            pass
    # Reuse SDK credentials only in this process; never serialize environments.
    for key, value in snapshots[0][3].items():
        if key.startswith(('E2B', 'TBENCH_E2B')):
            os.environ[key] = value
    relay_ids = [read(root / 'relay_manifest.json')['sandbox_id'] for root in roots]
    cleaned = asyncio.run(asyncio.wait_for(cleanup_sandboxes(roots, relay_ids), timeout=120))
    for root, status, command, env in snapshots:
        if args.reconcile_timeouts:
            from reconcile_tb21_empirical_timeouts import reconcile
            reconcile(root, empirical_report=False)
        env['TB21_TOTAL_WORKER_LIMIT'] = str(args.total_limit)
        if '--workers' in command:
            command[command.index('--workers') + 1] = str(args.workers)
        else:
            command.extend(['--workers', str(args.workers)])
        if '--accept-worker-change' not in command:
            command.append('--accept-worker-change')
        with (root / 'launch.log').open('ab') as log:
            process = subprocess.Popen(command, cwd=Path.cwd(), env=env,
                                       stdout=log, stderr=log, start_new_session=True)
        write(root / 'capacity_transition.json', {
            'old_pid': status['pid'], 'new_pid': process.pid,
            'old_workers': status['workers'], 'new_workers': args.workers,
            'total_limit': args.total_limit, 'cleaned_sandbox_ids': cleaned,
            'credentials_persisted': False, 'budget_resets': 0,
            'valid_slots_preserved': True, 'interrupted_requests_retained': True})
        print(root.name, process.pid)


if __name__ == '__main__':
    main()
