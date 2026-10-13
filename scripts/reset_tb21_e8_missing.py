"""Run only missing E8 trials under the user's explicit new budget epoch."""
import asyncio
import fcntl
import json
import os
from pathlib import Path
import sys
import subprocess

from audit_tb21_e8 import panel, merge_recovery
from recover_e8_verifier_slots import recovery_config
from tencent_pipeline_cache import write_json

REPO = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand')
REG = REPO / 'runs/tb21-e8-deepseek-max-1m-20261012'
JOB = Path('/data2/liyishan/tbench2-openclaw-min/jobs/tb21_skillexpand_20261012/tb21_e8_deepseek_max_1m_raw_20261012')
ROOT = REG / 'budget_reset_20261013'


async def recover(row):
    from harbor.models.trial.config import TrialConfig
    from harbor.trial.trial import Trial
    from harbor.trial.hooks import TrialEvent
    from tencent_task_verifier_cache import load, on_verification_started
    from tencent_pipeline_cache import on_verification_started as pipeline_hook
    from tencent_sandbox_terminus import TencentSandboxTerminus2
    from run_tencent_job import patch_result_agent_name
    versions = json.loads(os.environ['TB21_TASK_VERIFIER_CACHES'])
    if row['task'] in versions:
        load(row['task'], versions[row['task']])
    raw = json.loads(Path(row['result']).read_text())
    for attempt in (1, 2):
        directory = ROOT / f'attempt-{attempt}'
        dest = directory / row['trial']
        if (dest / 'recovery_intent.json').exists():
            raise RuntimeError('Budget epoch already dispatched: ' + row['trial'])
        trial = Trial(TrialConfig.model_validate(recovery_config(raw, directory)))
        if not isinstance(trial._agent, TencentSandboxTerminus2):
            raise RuntimeError('Incorrect recovery adapter')
        write_json(dest / 'recovery_intent.json', {'original_result': row['result'],
            'previous_executions': attempt - 1, 'execution_limit': 2,
            'remaining_after_reservation': 2 - attempt, 'budget_epoch': 'user-reset-20261013',
            'authorization_file': str(REG / 'budget_reset_20261013.json'),
            'historical_attempts_preserved': True, 'pid': os.getpid()})
        trial.add_hook(TrialEvent.VERIFICATION_START, on_verification_started)
        trial.add_hook(TrialEvent.VERIFICATION_START, pipeline_hook)
        await trial.run()
        patch_result_agent_name(trial.trial_dir)
        current = next(r for r in panel(directory, True) if r['trial'] == row['trial'])
        print(json.dumps({'trial': row['trial'], 'attempt': attempt, 'reward': current['reward'],
                          'issues': current['issues']}), flush=True)
        if current['reward'] is not None:
            return


def main():
    sys.path.insert(0, str(REPO / 'scripts'))
    rows = [r for r in panel(JOB, True) if r['reward'] is None]
    os.environ.update(HARBOR_E2B_FS_USER='root', TB21_TASK_VERIFIER_CACHES=json.dumps({
        name: 'repair-20261013-v2' for name in ['qemu-startup', 'qemu-alpine-ssh',
        'mteb-retrieve', 'torch-tensor-parallelism', 'pytorch-model-recovery']}))
    with (REG / 'recovery.lock').open('a') as own:
        fcntl.flock(own, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (REPO / 'runs/.tb21-worker-reservations.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            reserved = 0
            for path in (REPO / 'runs').glob('**/status.json'):
                try:
                    status = json.loads(path.read_text())
                except (ValueError, OSError):
                    continue
                if path.parent != ROOT and status.get('pid') and Path('/proc', str(status['pid'])).exists():
                    reserved += status.get('workers', 0)
            if reserved + len(rows) > 250:
                raise RuntimeError('Capacity unavailable')
            if (ROOT / 'status.json').exists():
                raise RuntimeError('Reset epoch already launched')
            write_json(ROOT / 'status.json', {'status': 'running', 'pid': os.getpid(),
                'workers': len(rows), 'missing_trials': len(rows), 'total_reserved': reserved + len(rows)})
        try:
            async def run():
                await asyncio.gather(*(recover(row) for row in rows))
            asyncio.run(run())
            command = [sys.executable, str(REPO / 'scripts/audit_tb21_e8.py'),
                '--job', str(JOB), '--baseline',
                '/data2/liyishan/tbench2-openclaw-min/jobs/tb21_skillexpand_20261002/tb21_e2_deepseek_raw_20261002',
                '--out', str(REG), '--recovery', str(ROOT / 'attempt-1'),
                '--recovery', str(ROOT / 'attempt-2')]
            subprocess.run(command, check=True)
        finally:
            write_json(ROOT / 'status.json', {'status': 'finished', 'workers': 0,
                'pid': os.getpid(), 'audit_required': True})


if __name__ == '__main__':
    main()
