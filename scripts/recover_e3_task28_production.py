"""Recover E3 task 28 through the production selector and Harbor L1 path."""
import json
import os
import shutil
from pathlib import Path

from omegaconf import OmegaConf
from skillexpand import schema as S
from skillexpand.runtime.llm_relay import relay_from_env
from skillexpand.runtime.parallel import ExperienceSpec, execute_experience
from skillexpand.benchmarks.terminalbench import audit_harbor_experience


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')


def main():
    root = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand/runs')
    src = root / 'tb21-e3-deepseek-parallel-20261006-recovery5'
    out = root / os.environ.get('TB21_E3_RECOVERY_RUN', 'tb21-e3-20261007-recovery7-production-task28')
    out.mkdir(exist_ok=False)
    for name in ('config.json', 'manifest.json', 'split.json', 'initial_skills.json', 'skills.jsonl', 'meta_skills.jsonl'):
        shutil.copy2(src / name, out / name)
    shutil.copytree(src / 'evolution/round-1/cards', out / 'evolution/round-1/cards',
                    ignore=lambda directory, names: ['28.json'] if '28.json' in names else [])
    skills = json.loads((src / 'evolution/round-1/input.json').read_text())['skills']
    save(out / 'manifest.recovery.json', {'stage': 'E3', 'source_run': str(src), 'task_ids': [28],
        'reused_tasks': [t for t in range(89) if t != 28], 'old_runs_read_only': True,
        'max_attempts': 3, 'max_workers': 100, 'persistence': 0})
    save(out / 'status.json', {'stage': 'E3', 'status': 'running', 'pid': os.getpid(), 'task_ids': [28]})
    (out / 'PID').write_text(str(os.getpid()) + '\n')
    relay = None
    try:
        relay = relay_from_env()
        url = relay.start()
        cfg = OmegaConf.load(out / 'config.json')
        cfg.benchmark.rollout.relay_base_url = url
        OmegaConf.save(cfg, out / 'runtime_config.json')
        os.environ.update(EXPE_CONFIG_FILE=str(out / 'runtime_config.json'), EXPE_LLM_BASE_URL=url,
                          TBENCH_RELAY_BASE_URL=url, TBENCH_PERSIST_SANDBOXES='0')
        spec = ExperienceSpec(unit_id='evolution:1:28', benchmark='terminalbench', task_id=28,
            family_id='unassigned', split='train', skill_library=tuple(skills), progressive_selection=True,
            selection_source=S.SELECTION_AGENT, max_trials=3, supervised_repair=False,
            frozen_selector_result=os.environ.get('TB21_FROZEN_SELECTOR_RESULT') or None,
            supervised_attempts=0, evolution_round=1,
            l1_checkpoint_path=str(out / 'evolution/round-1/trials/28.json'))
        result = execute_experience(spec)
        save(out / 'result.json', result)
        if not result.get('ok'):
            raise RuntimeError(result.get('error'))
        exp = S.from_dict(S.TaskExperience, result['experience'])
        assert exp.task_id == 28 and exp.num_trials == 3
        assert exp.skill_load['load_stage'] == 'after_selection'
        audit = audit_harbor_experience(exp)
        for trial in exp.l1_trials:
            path = Path(trial['trajectory'])
            assert path.is_file()
            raw = json.loads((path.parent.parent / 'result.json').read_text())
            assert not raw.get('exception_info'), raw.get('exception_info')
            if os.environ.get('TB21_REQUIRE_HARBOR_STREAMING') == '1':
                requests = [json.loads(line) for line in (path.parent / 'raw_model_requests.jsonl').read_text().splitlines()]
                assert requests and all(item.get('stream') is True for item in requests)
                activation = json.loads((path.parent / 'skill_activation.json').read_text())
                assert activation['load_stage'] == 'after_selection'
                assert Path(activation['source_path']).read_text() == skills[0]['body']
                trace = json.loads(path.read_text())
                steps = trace.get('steps', [])
                assert any(activation['skill_path'] in json.dumps(step) and step.get('source') != 'user'
                           for step in steps), 'Selected Skill has no execution trace evidence'
        save(out / 'evolution/round-1/cards/28.json', result['experience'])
        cards = [S.from_dict(S.TaskExperience, json.loads(p.read_text()))
                 for p in (out / 'evolution/round-1/cards').glob('*.json')]
        assert {c.task_id for c in cards} == set(range(89))
        save(out / 'audit.json', {'stage': 'E3', 'card_coverage': '89/89', 'task28_audit': audit,
             'selector': result.get('selection'), 'stage_complete': False,
             'reason': 'L1 recovery complete; full stage audit and L2 completion remain'})
        save(out / 'status.json', {'stage': 'E3', 'status': 'needs_attention', 'task28_recovery': 'complete',
             'card_coverage': '89/89', 'reason': 'Full stage audit and L2 completion remain'})
        (out / 'recovery_summary.md').write_text('# E3 task 28\n\nProduction L1 task recovery complete; 88 prior cards reused. Full stage acceptance remains pending.\n')
    except Exception as exc:
        save(out / 'status.json', {'stage': 'E3', 'status': 'needs_attention', 'error': str(exc),
                                  'error_class': getattr(exc, 'error_class', type(exc).__name__)})
        raise
    finally:
        if relay is not None:
            relay.close()


if __name__ == '__main__':
    main()
