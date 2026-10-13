"""Execute only the six remaining QEMU physical attempts, retaining originals."""
import argparse
import asyncio
import json
import os
import fcntl
from pathlib import Path

from tencent_pipeline_cache import write_json

REPO = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand')
REG = REPO / 'runs/tb21-e8-deepseek-max-1m-20261012'
JOBS = Path('/data2/liyishan/tbench2-openclaw-min/jobs/tb21_skillexpand_20261012')
JOB = JOBS / 'tb21_e8_deepseek_max_1m_raw_20261012'
ARCHIVE = JOBS / 'e8_retry_archives' / JOB.name
RECOVERY = REG / 'infrastructure_recovery_20261013'


def recovery_config(raw, recovery):
    config = dict(raw['config'])
    if config['agent'].get('import_path') != 'tencent_sandbox_terminus:TencentSandboxTerminus2':
        raise ValueError('Original Tencent adapter identity differs')
    # Harbor chooses a built-in name before an import_path in saved results.
    config['agent'] = dict(config['agent'], name=None)
    config.update(trial_name=raw['trial_name'], trials_dir=str(recovery))
    return config


async def recover(row):
    from harbor.models.trial.config import TrialConfig
    from harbor.trial.trial import Trial
    from harbor.trial.hooks import TrialEvent
    from tencent_task_verifier_cache import load, on_verification_started
    from tencent_pipeline_cache import on_verification_started as pipeline_hook
    trial_name = row['trial']
    source = JOB / trial_name
    destination = RECOVERY / trial_name
    intent = destination / 'recovery_intent.json'
    if intent.exists():
        raise RuntimeError('Recovery already reserved; inspect saved result instead of dispatching twice')
    raw = json.loads((source / 'result.json').read_text())
    histories = list((ARCHIVE / trial_name).glob('execution-*'))
    used = len(histories) + (0 if raw.get('exception_info') else 1)
    if used != 1 or not trial_name.startswith(('qemu-startup__', 'qemu-alpine-ssh__')):
        raise RuntimeError('No proven remaining QEMU attempt')
    version = 'repair-20261013-v2'
    load(raw['task_name'], version)
    config = recovery_config(raw, RECOVERY)
    validated = TrialConfig.model_validate(config)
    trial = Trial(validated)
    from tencent_sandbox_terminus import TencentSandboxTerminus2
    if not isinstance(trial._agent, TencentSandboxTerminus2):
        raise RuntimeError('Recovery did not select the original Tencent model adapter')
    destination.mkdir(parents=True, exist_ok=True)
    # This consumes the single remaining attempt before any agent dispatch.
    write_json(intent, {'original_result': str(source / 'result.json'), 'previous_executions': used,
                        'execution_limit': 2, 'remaining_after_reservation': 0, 'pid': os.getpid(),
                        'cache_version': version, 'model_parameters_unchanged': True})
    trial.add_hook(TrialEvent.VERIFICATION_START, on_verification_started)
    trial.add_hook(TrialEvent.VERIFICATION_START, pipeline_hook)
    outcome = await trial.run()
    from run_tencent_job import patch_result_agent_name
    patch_result_agent_name(trial.trial_dir)
    print(json.dumps({'trial': trial_name, 'reward': outcome.verifier_result.rewards if outcome.verifier_result else None,
                      'exception': outcome.exception_info.exception_type if outcome.exception_info else None}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--credential-pid', type=int, required=True)
    args = parser.parse_args()
    values = dict(v.split('=', 1) for v in Path(f'/proc/{args.credential_pid}/environ').read_bytes().decode().split('\0') if '=' in v)
    for key in ['E2B_API_KEY', 'TB21_METHOD_API_KEY']:
        if not values.get(key):
            raise ValueError('Authorized runtime credential unavailable')
    os.environ.update(E2B_API_KEY=values['E2B_API_KEY'], OPENAI_API_KEY=values['TB21_METHOD_API_KEY'],
        E2B_DOMAIN='ap-beijing.tencentags.com', HARBOR_E2B_FS_USER='root', TBENCH_PERSIST_SANDBOXES='0',
        TB21_TASK_VERIFIER_CACHES=json.dumps({t: 'repair-20261013-v2' for t in ['qemu-startup', 'qemu-alpine-ssh']}))
    audit = json.loads((REG / 'execution_audit.json').read_text())
    rows = [r for r in audit['rejected'] if r['task'] in ['qemu-startup', 'qemu-alpine-ssh']]
    with (REG / 'recovery.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (REPO / 'runs/.tb21-worker-reservations.lock').open('a') as capacity:
            fcntl.flock(capacity, fcntl.LOCK_EX)
            reserved = 0
            for status_path in (REPO / 'runs').glob('**/status.json'):
                try:
                    status = json.loads(status_path.read_text())
                    if status_path.parent != RECOVERY and status.get('pid') and Path('/proc', str(status['pid'])).exists():
                        reserved += status.get('workers', 0)
                except (OSError, ValueError):
                    continue
            if reserved + 6 > 250:
                raise ValueError('Shared worker capacity unavailable')
            write_json(RECOVERY / 'capacity.json', {'workers': 6, 'other_reserved': reserved, 'limit': 250})
            write_json(RECOVERY / 'status.json', {'status': 'running', 'pid': os.getpid(), 'workers': 6})
        write_json(RECOVERY / 'status.json', {'status': 'running', 'pid': os.getpid(), 'workers': 6})
        try:
            async def run():
                await asyncio.gather(*(recover(row) for row in rows))
            asyncio.run(run())
        finally:
            write_json(RECOVERY / 'status.json', {'status': 'finished', 'workers': 0})


if __name__ == '__main__':
    main()
