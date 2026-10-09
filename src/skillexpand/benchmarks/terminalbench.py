"""Minimal TerminalBench task table and deterministic smoke environment.

The real Harbor/Tencent rollout remains owned by SkillEvolve-TerminalBenchV2.
This adapter gives SkillExpand a stable task identity for imported trajectories
and makes accidental native execution fail explicitly instead of pretending to
have verifier semantics.
"""
import json
import subprocess
import os
from pathlib import Path
from .base import BaseEnv


def load_tasks(cfg):
    rows = json.loads(Path(cfg.benchmark.task_file).read_text())
    return [
        {
            "task": row["instruction"],
            "env_kwargs": {"instruction": row["instruction"], "task_name": row["task_name"]},
            "env_name": "terminalbench",
        }
        for row in rows
    ]


class TerminalBenchEnv(BaseEnv):
    """Guard environment; use the Harbor adapter for real rollouts."""
    def __init__(self, instruction, task_name="unknown", max_steps=1, **_):
        self.instruction = instruction
        self.task_name = task_name
        self.max_steps = max_steps
        self.reset()

    def reset(self):
        self.curr_step = 0
        self.terminated = False
        self.reward = False
        return self.instruction

    def step(self, action, *args, **kwargs):
        self.curr_step += 1
        self.terminated = True
        self.reward = False
        return ("Native SkillExpand execution is not available for TerminalBench; "
                "run the Harbor/Tencent adapter and import its verifier result.",
                False, True, False, self.curr_step)

    def success_fn(self):
        return bool(self.reward)


def harbor_rollout(cfg, task_id, skill, attempts, out_dir, evolution_round=0):
    """Run one task through the Tencent Harbor wrapper already used by TB2.1."""
    task = load_tasks(cfg)[task_id]
    rollout = cfg.benchmark.get('rollout', {})
    runner = Path(str(rollout.get('runner_script', ''))).expanduser()
    if not runner.is_file():
        raise FileNotFoundError(f'TerminalBench Tencent runner not found: {runner}')
    out_dir = Path(out_dir).resolve(); out_dir.mkdir(parents=True, exist_ok=True)
    task_name = task['env_kwargs']['task_name']
    skill_root = out_dir / 'skills'
    if skill and skill.body.strip():
        skill_path = skill_root / task_name / 'SKILL.md'
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(skill.body, encoding='utf-8')
    run_id = f'skillexpand_r{evolution_round}_task{task_id}_{os.getpid()}'
    jobs_dir = out_dir / 'jobs'
    env = os.environ.copy()
    env.update({
        'TBENCH_RUN_MODE': 'selected', 'TBENCH_TASK_NAMES': task_name,
        'TBENCH_EVAL_MODE': 'skills' if skill and skill.body.strip() else 'bare',
        'TBENCH_N_ATTEMPTS': str(int(attempts)), 'TBENCH_N_CONCURRENT': '1',
        'TBENCH_MAX_TRIAL_RETRIES': '0', 'RUN_ID': run_id,
        'JOBS_DIR': str(jobs_dir), 'TBENCH_SKILL_ROOT': str(skill_root), 'DRY_RUN': '0',
    })
    # Terminus runs inside the Tencent task sandbox.  A jinan40 loopback relay is
    # not visible from that sandbox; its model client already performs the HTTP
    # request inside E2B, so point it at the provider directly there.
    if rollout.get('llm_transport') == 'tencent_e2b_relay':
        env['MODEL_API_BASE'] = str(
            rollout.get('provider_base_url') or
            os.environ.get('TBENCH_RELAY_PROVIDER_BASE', 'https://llm-center.modelbest.co/v1'))
    log_path = out_dir / 'tencent_runner.log'
    completed = subprocess.run(['bash', str(runner)], cwd=str(runner.parent.parent),
                               capture_output=True, text=True, env=env)
    log_path.write_text(completed.stdout + '\n' + completed.stderr, encoding='utf-8')
    trial_dirs = sorted((jobs_dir / run_id).glob(f'{task_name}__*/'))
    trials = []
    for trial_dir in trial_dirs:
        result_path = trial_dir / 'result.json'
        if not result_path.is_file():
            continue
        result = json.loads(result_path.read_text(encoding='utf-8'))
        reward = (result.get('verifier_result') or {}).get('rewards', {}).get('reward')
        exception = result.get('exception_info') or {}
        trials.append({'attempt_index': len(trials) + 1, 'reward': reward,
                       'status': 'error' if exception else 'completed',
                       'exception_type': exception.get('exception_type'),
                       'trial_dir': str(trial_dir), 'result_path': str(result_path),
                       'trajectory_path': str(trial_dir / 'agent' / 'trajectory.json'),
                       'verifier_path': str(trial_dir / 'verifier')})
    if completed.returncode != 0 and not trials:
        raise RuntimeError(f'Tencent rollout failed: {completed.stderr[-2000:]}')
    return {'task_name': task_name, 'attempts': len(trials), 'trials': trials}, {
        'run_id': run_id, 'out_dir': str(out_dir), 'log_path': str(log_path),
        'returncode': completed.returncode,
    }


def audit_harbor_experience(exp):
    """Validate the external Harbor card without applying ExpeL checkpoint rules."""
    if exp.benchmark != 'terminalbench' or exp.experience_card is None:
        raise ValueError('not a TerminalBench experience')
    trials = list(exp.l1_trials)
    if not trials or [int(t['index']) for t in trials] != list(range(1, len(trials) + 1)):
        raise ValueError('TerminalBench attempts are not contiguous')
    rewards = tuple(bool(t.get('success')) for t in trials)
    if rewards != tuple(exp.trial_rewards) or exp.reward != any(rewards):
        raise ValueError('TerminalBench reward/card mismatch')
    if exp.experience_card.get('schema_version') != 5:
        raise ValueError('TerminalBench card is not schema-5')
    for trial in trials:
        if not trial.get('trajectory'):
            raise ValueError('TerminalBench trial has no trajectory path')
    return {'task_id': exp.task_id, 'trials': len(trials), 'reward': exp.reward,
            'skill_key': exp.initial_skill_key, 'audit': 'terminalbench-harbor-v1'}
