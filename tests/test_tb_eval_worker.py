"""Offline checks of the TB-eval worker, cold-start import, and stage launcher."""
import importlib.util
import json
import pickle
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from omegaconf import OmegaConf

from skillexpand import schema as S
from skillexpand.benchmarks.terminalbench import TerminalBenchEnv, audit_harbor_experience
from skillexpand.evaluation import progressive as PG
from skillexpand.l1 import artifacts as A
from skillexpand.l1 import workers as LW
from skillexpand.reliability.errors import JournalConflict
from skillexpand.runtime import agent_factory as F
from skillexpand.runtime import parallel as PL

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def skill(n, description, body=None):
    family = f'family-p{n:03d}'
    return S.Skill(f'terminalbench.{family}', family, 0, family, description,
                   body or f'SECRET BODY {n}',
                   S.Provenance(rationale='test'))


class Host:
    benchmark_name = 'terminalbench'

    def __init__(self, answer):
        self.answer, self.prompts = answer, []

    def llm(self, prompt, replace_newline=False):
        self.prompts.append(prompt)
        return self.answer


@pytest.fixture
def world(tmp_path, monkeypatch):
    (tmp_path / 'tasks.json').write_text(json.dumps(
        [{'task_name': 'tb-task', 'instruction': 'fix the failing test'}]))
    (tmp_path / 'trajectory.json').write_text(json.dumps(
        {'steps': [{'message': 'run pytest', 'observation': '1 failed'}]}))
    cfg = OmegaConf.create({'benchmark': {
        'name': 'terminalbench', 'task_file': str(tmp_path / 'tasks.json'),
        'rollout': {'mode': 'harbor_rollout', 'runner_script': str(tmp_path / 'r.sh')}}})
    library = (skill(1, 'git repair'), skill(2, 'package build'))
    calls = {}
    host = Host('SKILL: terminalbench.family-p002\nWHY: build task')

    def rollout(cfg_, task_id, selected, attempts, out_dir, evolution_round=0):
        calls.update(skill=selected, attempts=attempts, out_dir=out_dir)
        return ({'task_name': 'tb-task', 'attempts': 1, 'trials': [
            {'attempt_index': 1, 'reward': 1, 'status': 'completed',
             'trajectory_path': str(tmp_path / 'trajectory.json')}]},
                {'run_id': 'run-1', 'returncode': 0})

    monkeypatch.setattr(PL, '_config', lambda benchmark: cfg)
    monkeypatch.setattr(F, 'task_table', lambda c, refresh=False: [{'task': 'fix the failing test'}])
    monkeypatch.setattr(F, 'build_reasoning_host',
                        lambda c, usage_path=None, model=None, role=None:
                        calls.update(usage=usage_path, role=role) or host)
    monkeypatch.setattr('skillexpand.benchmarks.terminalbench.harbor_rollout', rollout)
    spec = PG.ProgressiveSpec(
        unit_id='r1:0', benchmark='terminalbench', task_id=0,
        skill_library=tuple(S.to_dict(s) for s in library), max_trials=3,
        l1_checkpoint_path=str(tmp_path / 'evolution/round-1/trials/0.json'),
        evolution_round=1)
    return spec, library, calls, host, tmp_path


def test_selected_body_reaches_rollout_and_identity_is_the_selected_skills(world):
    spec, library, calls, host, tmp = world
    record = PG.execute_progressive_experience(spec)
    assert record['ok'] and record['failure'] is None
    assert calls['skill'].skill_id == 'terminalbench.family-p002'
    assert calls['skill'].body == 'SECRET BODY 2'
    assert calls['attempts'] == 3
    assert calls['role'] == 'selector'
    assert calls['usage'] == str(tmp / 'evolution/round-1/trials/0.selector.json')
    exp = S.from_dict(S.TaskExperience, record['experience'])
    chosen = library[1]
    assert (exp.family_id, exp.initial_skill_key, exp.selected_skill_id) == (
        chosen.family_id, chosen.key, chosen.skill_id)
    assert exp.selection_source == S.SELECTION_AGENT
    assert exp.selection_reason == 'build task'
    assert exp.experience_card['task']['family_id'] == chosen.family_id
    assert record['family_id'] == chosen.family_id
    audit_harbor_experience(exp)
    assert record['skill_load'] == {'skill_id': chosen.skill_id, 'skill_key': chosen.key,
                                    'body_chars': len(chosen.body),
                                    'load_stage': 'after_selection'}
    selection = record['selection']
    assert selection['catalog_fingerprint'] == S.content_hash(PG.catalog(library))
    assert all(set(item) == {'skill_id', 'description'} for item in selection['catalog'])
    assert 'SECRET BODY' not in json.dumps(selection)
    assert 'SECRET BODY' not in '\n'.join(m.content for m in host.prompts[0])
    json.dumps(record)  # JSON-safe across the process boundary


def test_spec_is_picklable_and_catalog_is_body_free():
    spec = PG.ProgressiveSpec('u', 'terminalbench', 0, (S.to_dict(skill(1, 'd')),), '/x/0.json')
    assert pickle.loads(pickle.dumps(spec)) == spec
    assert PG.catalog([skill(1, 'd')]) == [{'skill_id': 'terminalbench.family-p001', 'description': 'd'}]


def test_selector_failure_is_reported_not_raised(world):
    spec, _, _, host, _ = world
    host.answer = 'SKILL: terminalbench.nope'
    record = PG.execute_progressive_experience(spec)
    assert record['ok'] is False and record['experience'] is None
    assert record['failure']['stage'] == 'l1'
    assert record['selection'] is None and record['skill_load'] is None


def test_rollout_failure_is_reported_not_raised(world, monkeypatch):
    spec = world[0]

    def boom(*a, **k):
        raise RuntimeError('harbor down')
    monkeypatch.setattr('skillexpand.benchmarks.terminalbench.harbor_rollout', boom)
    record = PG.execute_progressive_experience(spec)
    assert record['ok'] is False and 'harbor down' in record['failure']['message']


def test_non_harbor_benchmark_is_rejected_in_the_record(world, monkeypatch):
    spec = replace(world[0], benchmark='searchqa')
    record = PG.execute_progressive_experience(spec)
    assert record['ok'] is False


def test_fixed_worker_output_is_unchanged_by_the_shared_conversion(world):
    spec, library, calls, _, tmp = world
    fixed = LW.ExperienceSpec(
        unit_id='r1:0', benchmark='terminalbench', task_id=0, family_id='family-p001',
        split='train', skill_key=library[0].key, skill_body=library[0].body,
        skill_description='git repair', selected_skill_id=library[0].skill_id,
        selection_source=S.SELECTION_FIXED, max_trials=3,
        l1_checkpoint_path=spec.l1_checkpoint_path, evolution_round=1)
    record = LW.execute_experience(fixed)
    assert record['ok'] and record['family_id'] == 'family-p001'
    assert set(record) == {'record_type', 'unit_id', 'task_id', 'family_id', 'ok', 'experience',
                           'harbor_response', 'failure', 'pid', 'secs'}
    exp = S.from_dict(S.TaskExperience, record['experience'])
    assert exp.selection_source == S.SELECTION_FIXED and exp.initial_skill_key == library[0].key


def test_guard_environment_raises_instead_of_faking_an_observation():
    env = TerminalBenchEnv('do it', 'tb-task')
    with pytest.raises(RuntimeError, match='not available for TerminalBench'):
        env.step('ls')


#: Pinned on main before TB-eval; a change means every old card hash moved.
EXISTING_STYLE_HASH = 'fb61718dc0ab'


def test_existing_style_experience_serialization_is_unchanged():
    trials = ({'index': 1, 'phase': 'autonomous', 'status': 'completed', 'success': True,
               'termination': 'verifier', 'trajectory': '/t0'},)
    exp = S.TaskExperience(
        experience_id='discovery:0:28', benchmark='terminalbench', task_id=28, task='t',
        family_id='general', split='train', reward=True, num_trials=1,
        initial_skill_key='terminalbench.general@v0', trial_rewards=(True,),
        trial_phases=('autonomous',), experience_card={'schema_version': 5}, l1_trials=trials)
    assert [f for f in S.to_dict(exp)] == [
        'experience_id', 'benchmark', 'task_id', 'task', 'family_id', 'split', 'reward',
        'num_trials', 'initial_skill_key', 'failed_trajectories', 'reflections',
        'final_trajectory', 'selected_skill_id', 'selection_source', 'selection_reason',
        'selection_raw', 'trial_rewards', 'trial_phases', 'experience_card', 'l1_audit_path',
        'l1_trials', 'evolution_round']
    assert S.content_hash(S.to_dict(exp)) == EXISTING_STYLE_HASH


# ---- progressive load_cold_start ------------------------------------------------

def build_cold_start(tmp, tasks=2):
    names = [f'task-{i}' for i in range(tasks)]
    (tmp / 'tasks.json').write_text(json.dumps(
        [{'task_name': n, 'instruction': f'instruction of {n}'} for n in names]))
    source = tmp / 'raw'
    for n in names:
        trial = source / f'{n}__abc'
        (trial / 'agent').mkdir(parents=True)
        (trial / 'result.json').write_text(json.dumps(
            {'task_name': n, 'verifier_result': {'rewards': {'reward': 1}}}))
        (trial / 'agent/trajectory.json').write_text(json.dumps(
            {'steps': [{'message': 'ls', 'observation': 'files'}]}))
    root = tmp / 'run'
    importer = load_script('import_terminalbench_batch')
    importer.main(['--source-root', str(source), '--task-file', str(tmp / 'tasks.json'),
                   '--run-dir', str(root), '--expected-tasks', str(tasks), '--attempts', '1',
                   '--runner-script', str(tmp / 'r.sh')])
    return root


def test_importer_writes_a_progressive_cold_start_that_loads(tmp_path):
    root = build_cold_start(tmp_path)
    config = json.loads((root / 'config.json').read_text())
    assert config['benchmark']['progressive_library'] is True
    assert not (root / 'clusters.json').exists() and not (root / 'task_skill_map.json').exists()
    cfg, plan, skills, cards = A.load_cold_start(root)
    assert sorted(cards) == [0, 1] and len(skills) == 1
    assert all(c.family_id == 'general' and c.initial_skill_key is None for c in cards.values())
    from skillexpand.persistence.store import SkillLibrary
    assert SkillLibrary(root / 'skills.jsonl', benchmark='terminalbench').families


def test_imported_config_survives_the_launcher_role_overrides_unchanged(tmp_path):
    """The stage launcher re-resolves the role map and re-freezes config.json; the importer's
    frozen config must already be that exact fixed point or the first launch raises
    FrozenProtocolChanged (a missing ``l2_verifier`` role did exactly that)."""
    from omegaconf import OmegaConf
    from skillexpand.cli import apply_model_overrides, build_parser
    from skillexpand.persistence.io import freeze
    root = build_cold_start(tmp_path)
    model = json.loads((root / 'config.json').read_text())['agent']['llm']
    flags = ['--l1-model', model, '--cold-start-model', model, '--l2-planner-model', model,
             '--l2-editor-model', model, '--l2-reviewer-model', model, '--selector-model', model]
    args = build_parser().parse_args(['--run-dir', str(root), *flags])
    cfg = OmegaConf.create(json.loads((root / 'config.json').read_text()))
    resolved = OmegaConf.to_container(apply_model_overrides(cfg, args), resolve=True)
    freeze(root / 'config.json', resolved)


def rewrite(path, mutate):
    value = json.loads(path.read_text())
    path.write_text(json.dumps(mutate(value) or value))


@pytest.mark.parametrize('name,mutate,match', [
    ('cold_start_complete.json', lambda v: v.update(protocol='other'), 'protocol'),
    ('cold_start_complete.json', lambda v: v.update(train_count=5), 'train count'),
    ('cold_start_complete.json', lambda v: v.update(initial_skills_hash='x'), 'hash mismatch'),
    ('discovery/results/0.json', lambda v: v.update(initial_skill_key='terminalbench.general@v0'),
     'Invalid cold-start experience'),
    ('discovery/results/0.json', lambda v: v['experience_card'].update(schema_version=4),
     'Invalid cold-start experience'),
    ('discovery/results/0.json', lambda v: v['experience_card']['task'].update(text='tampered'),
     'differ from discovery hashes'),
    ('discovery/card_hashes.json', lambda v: v.update({'0': 'bad'}), 'differ from discovery hashes'),
    ('manifest.json', lambda v: v.update(task_table_hash='bad'), 'task data changed'),
    ('initial_skills.json', lambda v: v[0].update(body='tampered'), 'hash mismatch'),
])
def test_progressive_load_cold_start_rejects_tampering(tmp_path, name, mutate, match):
    root = build_cold_start(tmp_path)
    rewrite(root / name, mutate)
    with pytest.raises(JournalConflict, match=match):
        A.load_cold_start(root)


def test_progressive_load_cold_start_rejects_an_empty_skill_body_even_if_rehashed(tmp_path):
    root = build_cold_start(tmp_path)
    initial = json.loads((root / 'initial_skills.json').read_text())
    initial[0]['body'] = ' '
    (root / 'initial_skills.json').write_text(json.dumps(initial))
    rewrite(root / 'cold_start_complete.json',
            lambda v: v.update(initial_skills_hash=S.content_hash(initial)))
    with pytest.raises(JournalConflict, match='Invalid initial Skill'):
        A.load_cold_start(root)


def test_progressive_load_cold_start_requires_core_files(tmp_path):
    root = build_cold_start(tmp_path)
    (root / 'initial_skills.json').unlink()
    with pytest.raises(JournalConflict, match='missing'):
        A.load_cold_start(root)


def test_materialized_model_library_replaces_bootstrap_once_and_still_loads(tmp_path):
    root = build_cold_start(tmp_path)
    proposal = tmp_path / 'proposal.json'
    proposal.write_text(json.dumps({'skills': [
        {'skill_id': 'terminalbench.family-p001', 'family_id': 'family-p001', 'name': 'a',
         'description': 'git repair', 'trigger_conditions': [], 'body': '1. x'},
        {'skill_id': 'terminalbench.family-p002', 'family_id': 'family-p002', 'name': 'b',
         'description': 'package build', 'trigger_conditions': [], 'body': '1. y'}]}))
    materialize = load_script('materialize_terminalbench_library')
    argv = sys.argv
    sys.argv = ['m', '--run-dir', str(root), '--proposal', str(proposal)]
    try:
        assert materialize.main() == 0
        assert materialize.main() == 0  # idempotent for the identical proposal
    finally:
        sys.argv = argv
    _, _, skills, cards = A.load_cold_start(root)
    assert [s.skill_id for s in skills] == ['terminalbench.family-p001', 'terminalbench.family-p002']
    assert all(c.family_id == 'general' for c in cards.values())
    assert json.loads((root / 'library_manifest.json').read_text())['catalog_has_body'] is False


def test_main_branch_still_demands_cluster_files(tmp_path):
    root = build_cold_start(tmp_path)
    config = json.loads((root / 'config.json').read_text())
    del config['benchmark']['progressive_library']
    (root / 'config.json').write_text(json.dumps(config))
    with pytest.raises(FileNotFoundError):
        A.load_cold_start(root)


# ---- stage launcher ---------------------------------------------------------------

def test_launch_command_equals_the_registered_spec(tmp_path):
    stage = load_script('tb_eval_stage')
    cmd = stage.build_launch_command('py', tmp_path, 'exec-model', 'method-model', 100)
    assert cmd == [
        'py', '-m', 'skillexpand', '--benchmark', 'terminalbench', '--run-dir', str(tmp_path),
        '--phase', 'evolve', '--resume', '--progressive-library',
        '--acceptance-mode', 'predicted', '--skill-edit-mode', 'rewrite',
        '--evolve-rounds', '1', '--candidate-count', '3', '--batch-size', '50',
        '--autonomous-attempts', '3', '--supervised-attempts', '0',
        '--evolve-l1-workers', '100', '--l2-review-workers', '100', '--test-workers', '100',
        '--l1-model', 'exec-model', '--cold-start-model', 'method-model',
        '--l2-planner-model', 'method-model', '--l2-editor-model', 'method-model',
        '--l2-reviewer-model', 'method-model', '--selector-model', 'method-model',
        '--llm-relay']
    assert stage.launch_env({'A': '1'}) == {'A': '1', 'TBENCH_PERSIST_SANDBOXES': '0'}
    assert stage.build_launch_command('py', tmp_path, 'e', 'm', 0)[
        cmd.index('--evolve-l1-workers') + 1] == '1'


def test_launch_spawns_detached_and_writes_the_pidfile(tmp_path, monkeypatch):
    stage = load_script('tb_eval_stage')
    (tmp_path / 'input_coverage.json').write_text(json.dumps({'complete_valid_coverage': True}))
    seen = {}

    class Proc:
        pid = 4242

    def popen(cmd, **kwargs):
        seen.update(cmd=cmd, **kwargs)
        return Proc()
    monkeypatch.setattr(stage.subprocess, 'Popen', popen)
    monkeypatch.setenv('TB21_WORKERS', '7')
    args = stage.argparse.Namespace(
        run_dir=tmp_path, stage='E3', python='py', executor_model='e', method_model='m')
    assert stage.launch(args) == 0
    assert seen['start_new_session'] is True
    assert seen['env']['TBENCH_PERSIST_SANDBOXES'] == '0'
    assert seen['cmd'][seen['cmd'].index('--test-workers') + 1] == '7'
    assert (tmp_path / 'stage.pid').read_text() == '4242\n'


def test_launch_is_blocked_without_complete_coverage(tmp_path):
    stage = load_script('tb_eval_stage')
    (tmp_path / 'input_coverage.json').write_text(json.dumps({'complete_valid_coverage': False}))
    args = stage.argparse.Namespace(
        run_dir=tmp_path, stage='E4', python='py', executor_model='e', method_model='m')
    assert stage.launch(args) == 2
    assert not (tmp_path / 'stage.pid').exists()
