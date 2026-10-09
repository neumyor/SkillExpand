"""Offline checks of the progressive library wired through the evolution loop and audit."""
import json
import sys
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from omegaconf import OmegaConf

from skillexpand import schema as S
from skillexpand.evaluation import progressive as PG
from skillexpand.evaluation import routing as R
from skillexpand.l1 import artifacts as A
from skillexpand.l1 import cold_start as C
from skillexpand.l1 import workers as LW
from skillexpand.l2 import audit as AU
from skillexpand.l2 import loop as L
from skillexpand.persistence.io import AuditFailure
from skillexpand.reliability.errors import (
    FrozenCodeChanged, FrozenProtocolChanged, InvalidInput, JournalConflict,
)
from skillexpand.runtime import agent_factory as F
from skillexpand.runtime import parallel as PL
from tests import test_experience_first as fixtures
from tests import test_serial_l2 as serial
from tests.test_tb_eval_worker import build_cold_start, load_script

TASKS = 4
CONFIG = dict(batch_size=50, candidate_count=3, skill_edit_mode='rewrite',
              reviewer_update_mode='none', autonomous_attempts=1, supervised_attempts=0)


def progressive_config(**overrides):
    return L.EvolutionConfig(**{**CONFIG, 'progressive_library': True, **overrides})


def editor_llm(messages, **kw):
    payload = json.loads(messages[-1].content)
    if isinstance(payload, list):
        return json.dumps({'patterns': []})
    if 'K' in payload:
        card = payload['cards'][0]
        return json.dumps({'hypotheses': [{
            'mechanism': name, 'change': name,
            'evidence': [{'card_id': card['card_id'],
                          'evidence_id': card['evidence'][0]['id']}]}
            for name in ['inspect evidence', 'remove fallback'][:payload['K']]]})
    current = json.loads(messages[-2].content)['current_skill']
    return json.dumps({'description': current['description'],
                       'body': 'NEW ' + payload['selected_hypothesis']['mechanism']})


def judge_llm(messages, **kw):
    body = json.loads(messages[-1].content)['skill']['body']
    return json.dumps({'probability_true': 0.8 if body.startswith('NEW ') else 0.2,
                       'predicted_success': body.startswith('NEW '), 'reason': 'test forecast'})


def selector_llm(messages, **kw):
    task = messages[-1].content.rsplit('TASK:\n', 1)[1]
    family = 'p001' if int(task[-1]) % 2 == 0 else 'p002'
    return f'SKILL: terminalbench.family-{family}\nWHY: parity'


class World:
    def __init__(self, tmp, monkeypatch):
        self.tmp, self.monkeypatch = tmp, monkeypatch
        self.root = build_cold_start(tmp, tasks=TASKS)
        proposal = tmp / 'proposal.json'
        proposal.write_text(json.dumps({'skills': [
            {'skill_id': 'terminalbench.family-p001', 'family_id': 'family-p001', 'name': 'a',
             'description': 'even tasks', 'trigger_conditions': [], 'body': '1. x'},
            {'skill_id': 'terminalbench.family-p002', 'family_id': 'family-p002', 'name': 'b',
             'description': 'odd tasks', 'trigger_conditions': [], 'body': '1. y'}]}))
        materialize = load_script('materialize_terminalbench_library')
        argv = sys.argv
        sys.argv = ['m', '--run-dir', str(self.root), '--proposal', str(proposal)]
        try:
            assert materialize.main() == 0
        finally:
            sys.argv = argv
        (tmp / 'trajectory.json').write_text(json.dumps(
            {'steps': [{'message': 'run pytest', 'observation': '1 failed'}]}))
        self.cfg, self.plan, _, _ = A.load_cold_start(self.root)
        self.rolled, self.l1_runs, self.crash_task = [], [], None
        self.hosts = {'selector': SimpleNamespace(
            token_counter=len, llm=selector_llm, benchmark_name='terminalbench'),
            'l2_planner': SimpleNamespace(token_counter=len, llm=editor_llm),
            'l2_editor': SimpleNamespace(token_counter=len, llm=editor_llm),
            'l2_reviewer': SimpleNamespace(token_counter=len, llm=judge_llm)}

    def rollout(self, cfg, task_id, selected, attempts, out_dir, evolution_round=0):
        self.rolled.append((task_id, selected.skill_id, selected.body))
        return ({'task_name': f'task-{task_id}', 'attempts': 1, 'trials': [
            {'attempt_index': 1, 'reward': task_id % 2, 'status': 'completed',
             'trajectory_path': str(self.tmp / 'trajectory.json')}]},
                {'run_id': f'run-{task_id}', 'returncode': 0})

    def units(self, specs, worker, workers, on_result, **kw):
        for spec in specs:
            if isinstance(spec, PG.ProgressiveSpec):
                assert worker is PG.execute_progressive_experience
                self.l1_runs.append(spec.task_id)
                if spec.task_id == self.crash_task:
                    on_result({'record_type': 'experience', 'unit_id': spec.unit_id,
                               'task_id': spec.task_id, 'ok': False, 'experience': None,
                               'failure': {'category': 'provider_unavailable', 'stage': 'l1',
                                           'unit_id': spec.task_id, 'message': 'down',
                                           'type': 'RuntimeError', 'retryable': True}})
                    continue
            else:
                assert worker is not LW.execute_experience, 'fixed worker used under progressive'
            on_result(worker(spec))

    def patched(self):
        host = lambda cfg, path=None, model=None, role=None: self.hosts[role]  # noqa: E731
        return [
            patch.object(PL, '_config', lambda benchmark: self.cfg),
            patch.object(PL, 'run_generic', side_effect=self.units),
            patch.object(F, 'build_reasoning_host', side_effect=host),
            patch('skillexpand.benchmarks.terminalbench.harbor_rollout', self.rollout)]

    def run(self, config=None, rounds=1):
        contexts = self.patched()
        for c in contexts:
            c.start()
        try:
            loop = L.SerialEvolutionLoop(self.cfg, self.plan, L.LoopPaths(self.root),
                                         config or progressive_config())
            return loop, loop.run_evolutions(rounds)
        finally:
            for c in reversed(contexts):
                c.stop()

    def loop(self, config=None):
        return L.SerialEvolutionLoop(self.cfg, self.plan, L.LoopPaths(self.root),
                                     config or progressive_config())

    @property
    def round_dir(self):
        return self.root / 'evolution' / 'round-1'


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def test_progressive_round_end_to_end_audit_and_model_free_resume(world):
    loop, summary = world.run()
    assert summary['status'] == 'complete' and summary['train_cards'] == TASKS
    assert summary['val_executions'] == 0
    assert sorted(world.l1_runs) == [0, 1, 2, 3]
    # The selected Skill's body is what the rollout got.
    assert sorted((t, sid) for t, sid, _ in world.rolled) == [
        (0, 'terminalbench.family-p001'), (1, 'terminalbench.family-p002'),
        (2, 'terminalbench.family-p001'), (3, 'terminalbench.family-p002')]
    assert {b for _, sid, b in world.rolled if sid.endswith('p001')} == {'1. x'}
    directory = world.round_dir
    assert {p.name for p in (directory / 'cards').glob('*.json')} == {f'{t}.json' for t in range(4)}
    assert {p.name for p in (directory / 'selection').glob('*.json')} == {f'{t}.json' for t in range(4)}
    manifest = json.loads((directory / 'manifest.json').read_text())
    assert set(manifest['routes']) == {'0', '1', '2', '3'}
    assert set(manifest['routes']['0']) == {'skill_id', 'skill_key', 'load_stage',
                                            'catalog_fingerprint'}
    assert manifest['routes']['1']['skill_id'] == 'terminalbench.family-p002'
    assert 'raw' not in json.dumps(manifest['routes'])
    sidecar = json.loads((directory / 'selection' / '0.json').read_text())
    assert sidecar['selection']['raw'].startswith('SKILL:')
    assert 'body' not in json.dumps(sidecar['selection']['catalog'])
    # The card record carries no sidecar field: card serialization is unchanged.
    card = json.loads((directory / 'cards' / '0.json').read_text())
    assert 'selection' not in card and 'skill_load' not in card
    # Batches follow the selected Skill; the panel is the closed train set.
    batches = json.loads((directory / 'batches.json').read_text())
    assert sorted((b['skill_id'], tuple(b['task_ids'])) for b in batches) == [
        ('terminalbench.family-p001', (0, 2)), ('terminalbench.family-p002', (1, 3))]
    assert (world.root / 'routes' / 'train' / 'complete.json').exists()
    assert not (world.root / 'routes' / 'val').exists()
    assert (world.root / 'train' / 'predicted_scores.jsonl').exists()
    assert not (world.root / 'val').exists()
    for path in (world.root / 'l2_batches').glob('*.json'):
        value = json.loads(path.read_text())
        assert value['acceptance']['executions'] == 0
        assert value['outcome'] == 'review_approved' and value['candidate']
    assert {k: v for k, v in summary['skills'].items()} == {
        sid: f'{sid}@v1' for sid in ('terminalbench.family-p001', 'terminalbench.family-p002')}
    frozen = json.loads((world.root / 'l2_manifest.json').read_text())
    assert frozen['config']['progressive_library'] is True
    AU.audit_round(world.root, 1)

    before = (directory / 'manifest.json').read_bytes()
    world.hosts = {k: SimpleNamespace(token_counter=len, benchmark_name='terminalbench',
                                      llm=lambda *a, **k: (_ for _ in ()).throw(
                                          AssertionError('model call on resume')))
                   for k in world.hosts}
    with patch.object(PL, '_config', lambda benchmark: world.cfg), \
            patch.object(PL, 'run_generic', side_effect=AssertionError('work on resume')), \
            patch.object(F, 'build_reasoning_host', side_effect=AssertionError('host on resume')):
        restored = world.loop()
        assert restored.run_evolutions(1) == summary
    assert (directory / 'manifest.json').read_bytes() == before


def test_mid_round_crash_then_resume_keeps_routes_byte_identical(world, tmp_path_factory):
    world.crash_task = 3
    with pytest.raises(Exception, match='down'):
        world.run()
    assert sorted(world.l1_runs) == [0, 1, 2, 3]
    assert not (world.round_dir / 'manifest.json').exists()
    assert {p.name for p in (world.round_dir / 'selection').glob('*.json')} >= {
        '0.json', '1.json', '2.json'}
    world.crash_task, world.l1_runs = None, []
    loop, summary = world.run()
    assert world.l1_runs == [3]   # only the unfinished task is re-selected and re-run
    resumed = json.loads((world.round_dir / 'manifest.json').read_text())['routes']
    assert resumed == AU.progressive_routes(world.round_dir, [0, 1, 2, 3])

    clean = World(tmp_path_factory.mktemp('clean'), world.monkeypatch)
    clean.run()
    reference = json.loads((clean.round_dir / 'manifest.json').read_text())['routes']
    assert json.dumps(resumed) == json.dumps(reference)
    AU.audit_round(world.root, 1)


def test_crash_after_manifest_freeze_leaves_manifest_bytes_unchanged(world):
    with patch.object(L.SerialEvolutionLoop, '_run_batch', side_effect=RuntimeError('boom')):
        with pytest.raises(RuntimeError, match='boom'):
            world.run()
    before = (world.round_dir / 'manifest.json').read_bytes()
    world.l1_runs = []
    _, summary = world.run()
    assert world.l1_runs == []
    assert (world.round_dir / 'manifest.json').read_bytes() == before
    assert summary['status'] == 'complete'


def test_completed_route_is_reloaded_not_rebuilt_when_the_provider_identity_moves(world):
    world.run()
    routes_manifest = world.root / 'routes' / 'train' / 'manifest.json'
    frozen = json.loads(routes_manifest.read_text())
    frozen['provider'] = {'moved': 'port'}
    routes_manifest.write_text(json.dumps(frozen))
    scorer = None
    contexts = world.patched()
    for c in contexts:
        c.start()
    try:
        scorer = world.loop()._ensure_predicted_scorer()
    finally:
        for c in reversed(contexts):
            c.stop()
    assert scorer.routes.split == S.SPLIT_TRAIN
    assert scorer.routes.groups == {'terminalbench.family-p001': (0, 2),
                                    'terminalbench.family-p002': (1, 3)}


def test_load_existing_still_checks_descriptions_for_train_routes(world):
    world.run()
    library = [replace(s, description='changed') for s in world.loop().initial]
    with pytest.raises(JournalConflict, match='descriptions'):
        R.FrozenRoutes.load_existing(world.cfg, world.plan, library, world.root / 'routes',
                                     S.SPLIT_TRAIN)


def test_rejected_candidates_hold_the_head_and_still_audit(world):
    world.hosts['l2_reviewer'] = SimpleNamespace(
        token_counter=len, llm=lambda messages, **kw: json.dumps(
            {'probability_true': 0.1, 'predicted_success': False, 'reason': 'no'}))
    _, summary = world.run()
    assert summary['review_approved_updates'] == 0
    assert summary['skills'] == {sid: f'{sid}@v0' for sid in (
        'terminalbench.family-p001', 'terminalbench.family-p002')}
    AU.audit_round(world.root, 1)


def test_audit_rejects_tampered_selection_records(world):
    world.run()
    path = world.round_dir / 'selection' / '0.json'
    original = path.read_text()

    def tamper(mutate):
        value = json.loads(original)
        mutate(value)
        path.write_text(json.dumps(value))
        with pytest.raises(AuditFailure):
            AU.audit_round(world.root, 1)
        path.write_text(original)

    tamper(lambda v: v['skill_load'].update(load_stage='before_selection'))
    tamper(lambda v: v['skill_load'].update(skill_id='terminalbench.family-p002'))
    tamper(lambda v: v['selection']['catalog'][0].update(body='leak'))
    tamper(lambda v: v['selection'].update(catalog_fingerprint='x'))
    AU.audit_round(world.root, 1)
    path.unlink()
    with pytest.raises(AuditFailure):
        AU.audit_round(world.root, 1)


def test_audit_rejects_a_card_that_claims_a_fixed_selection(world):
    world.run()
    path = world.round_dir / 'cards' / '0.json'
    value = json.loads(path.read_text())
    value['selection_source'] = S.SELECTION_FIXED
    path.write_text(json.dumps(value))
    with pytest.raises(AuditFailure):
        AU.audit_round(world.root, 1)


# ---- misconfiguration ------------------------------------------------------------

@pytest.mark.parametrize('kwargs', [
    dict(acceptance_mode='sampled', skill_edit_mode='structured'),
    dict(acceptance_mode='empirical'),
    dict(acceptance_mode='jev'),
    dict(predicted_review_scope='train_cards'),
])
def test_progressive_needs_predicted_val_acceptance(kwargs):
    with pytest.raises(InvalidInput, match='progressive_library requires'):
        progressive_config(**kwargs)


def test_progressive_switch_must_match_the_frozen_cold_start(world):
    with pytest.raises(InvalidInput, match='rerun with --progressive-library'):
        world.loop(replace(progressive_config(), progressive_library=False))
    config_path = world.root / 'config.json'
    frozen = json.loads(config_path.read_text())
    del frozen['benchmark']['progressive_library']
    config_path.write_text(json.dumps(frozen))
    with pytest.raises(InvalidInput, match='not progressive'):
        world.loop()


def test_progressive_requires_terminalbench_harbor(world):
    other = OmegaConf.create(OmegaConf.to_container(world.cfg, resolve=True))
    other.benchmark.name = 'searchqa'
    with pytest.raises(InvalidInput, match='terminalbench'):
        L.SerialEvolutionLoop(other, world.plan, L.LoopPaths(world.root), progressive_config())
    other = OmegaConf.create(OmegaConf.to_container(world.cfg, resolve=True))
    other.benchmark.rollout.mode = 'native'
    with pytest.raises(InvalidInput, match='harbor_rollout'):
        L.SerialEvolutionLoop(other, world.plan, L.LoopPaths(world.root), progressive_config())


def test_progressive_requires_no_reviewer_calibration(world):
    with pytest.raises(InvalidInput, match='reviewer_update_mode=none'):
        world.loop(progressive_config(reviewer_update_mode='rules'))


def test_cli_flag_reaches_the_evolution_config():
    from skillexpand import cli
    parser = cli.build_parser()
    base = ['--benchmark', 'terminalbench', '--run-dir', '/x']
    assert parser.parse_args(base + ['--progressive-library']).progressive_library is True
    assert parser.parse_args(base).progressive_library is False


# ---- main identity is unchanged --------------------------------------------------

#: The EvolutionConfig keys main froze into l2_manifest.json before TB-eval.
MAIN_IDENTITY_KEYS = [
    'acceptance_confidence', 'acceptance_mode', 'acceptance_sample_size', 'autonomous_attempts',
    'batch_size', 'candidate_count', 'claim_verification', 'evolve_l1_workers',
    'l2_review_workers', 'planner_memory_mode', 'predicted_review_scope',
    'reviewer_feedback_size', 'reviewer_memory_mode', 'reviewer_update_mode',
    'single_candidate', 'skill_edit_mode', 'supervised_attempts']


class MainIdentityTests(unittest.TestCase):
    setUp = fixtures.ExperienceFirstTests.setUp
    tearDown = fixtures.ExperienceFirstTests.tearDown
    cold = fixtures.ExperienceFirstTests.cold
    ask = fixtures.ExperienceFirstTests.ask
    units = serial.SerialL2Tests.units

    def test_default_off_switch_leaves_the_frozen_l2_identity_and_family_cards_unchanged(self):
        plan = self.cold().run()
        C.freeze(self.root / 'config.json', OmegaConf.to_container(self.cfg, resolve=True))
        loop = L.SerialEvolutionLoop(
            self.cfg, plan, L.LoopPaths(self.root),
            L.EvolutionConfig(batch_size=1, predicted_review_scope='train_cards',
                              candidate_count=3, skill_edit_mode='rewrite',
                              reviewer_update_mode='none'))
        identity = json.loads((self.root / 'l2_manifest.json').read_text())
        self.assertEqual(sorted(identity['config']), MAIN_IDENTITY_KEYS)
        self.assertNotIn('progressive_library', identity['config'])
        self.assertTrue(all(c.family_id == plan.family_of(t) for t, c in loop.cards.items()))
        self.assertFalse(loop.config.progressive_library)
        self.assertEqual(L.EvolutionConfig().progressive_library, False)


# ---- relay port drift on resume --------------------------------------------------

RELAY_A, RELAY_B = 'http://127.0.0.1:41001/v1', 'http://127.0.0.1:41002/v1'


def use_relay(world, url, required=True):
    """Do what cli does for a relay launch: new endpoint env + rewritten frozen config."""
    world.monkeypatch.setenv('EXPE_LLM_BASE_URL', url)
    world.monkeypatch.setenv('OPENAI_API_BASE', url)
    if required:
        world.monkeypatch.setenv('EXPE_LLM_RELAY_REQUIRED', '1')
    else:
        world.monkeypatch.delenv('EXPE_LLM_RELAY_REQUIRED', raising=False)
    config = json.loads((world.root / 'config.json').read_text())
    config['benchmark']['rollout'].update(
        llm_transport='tencent_e2b_relay', relay_base_url=url, direct_provider_fallback=False)
    (world.root / 'config.json').write_text(json.dumps(config))
    manifest = json.loads((world.root / 'manifest.json').read_text())
    manifest['config'] = config
    (world.root / 'manifest.json').write_text(json.dumps(manifest))
    world.cfg, world.plan, _, _ = A.load_cold_start(world.root)


def crash_in_judge(world):
    good = world.hosts['l2_reviewer']
    world.hosts['l2_reviewer'] = SimpleNamespace(
        token_counter=len, llm=lambda *a, **k: (_ for _ in ()).throw(RuntimeError('judge down')))
    with pytest.raises(Exception, match='judge down'):
        world.run()
    world.hosts['l2_reviewer'] = good


def test_relay_run_resumes_across_a_relay_port_change(world):
    use_relay(world, RELAY_A)
    crash_in_judge(world)
    assert (world.root / 'routes' / 'train' / 'complete.json').exists()
    frozen_l2 = (world.root / 'l2_manifest.json').read_bytes()
    use_relay(world, RELAY_B)
    world.l1_runs = []
    loop, summary = world.run()
    assert summary['status'] == 'complete' and world.l1_runs == []
    assert (world.root / 'l2_manifest.json').read_bytes() == frozen_l2   # never rewritten
    AU.audit_round(world.root, 1)


def test_relay_port_change_during_routing_resumes_the_partial_route(world):
    use_relay(world, RELAY_A)
    calls = []

    def flaky_selector(messages, **kw):
        calls.append(1)
        if len(calls) > 2:
            raise RuntimeError('selector down')
        return selector_llm(messages)
    world.hosts['selector'] = SimpleNamespace(
        token_counter=len, llm=flaky_selector, benchmark_name='terminalbench')
    with pytest.raises(Exception):
        world.run()
    assert not (world.root / 'routes' / 'train' / 'complete.json').exists()
    world.hosts['selector'] = SimpleNamespace(
        token_counter=len, llm=selector_llm, benchmark_name='terminalbench')
    use_relay(world, RELAY_B)
    _, summary = world.run()
    assert summary['status'] == 'complete'


def test_relay_port_change_never_waives_source_code_drift(world):
    use_relay(world, RELAY_A)
    crash_in_judge(world)
    use_relay(world, RELAY_B)
    with patch('skillexpand.l2.loop.code_signature', return_value={'x.py': 'drifted'}):
        with pytest.raises(FrozenCodeChanged):
            world.run()
        with patch.object(L.SerialEvolutionLoop, 'run_evolutions', lambda self, rounds=None: None):
            L.SerialEvolutionLoop(world.cfg, world.plan, L.LoopPaths(world.root),
                                  progressive_config(), allow_code_change=True)
    ledger = (world.root / 'code_changes.jsonl').read_text()
    assert 'l2_manifest.json' in ledger


def test_non_relay_provider_drift_is_still_refused(world):
    use_relay(world, RELAY_A, required=False)
    crash_in_judge(world)
    use_relay(world, RELAY_B, required=False)
    with pytest.raises(FrozenProtocolChanged):
        world.run()


def test_relay_run_still_refuses_a_real_protocol_change(world):
    use_relay(world, RELAY_A)
    crash_in_judge(world)
    use_relay(world, RELAY_B)
    with pytest.raises(FrozenProtocolChanged):
        world.run(progressive_config(candidate_count=2))
