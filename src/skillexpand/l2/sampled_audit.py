"""Offline integrity checks for batches decided by the sampled protocol.

Each check names a way the protocol could quietly stop being the protocol.
They read only the journals: no model, no environment, no writes.  The
numbering follows the audit invariants of
``docs/EXPERIMENT_PLAN_PLANNER_REVIEWER_COEVOLVE.md`` section 7.
"""

from skillexpand import schema as S
from skillexpand.evaluation import ppi as PPI
from skillexpand.evaluation.claim_check import CATEGORIES
from skillexpand.l2 import ledger as LED
from skillexpand.l2 import memory as MEM
from skillexpand.persistence.io import require

TOLERANCE = 1e-9


def _close(stored, value):
    if stored is None or value is None:
        return stored is None and value is None
    return abs(float(stored) - float(value)) < TOLERANCE


def audit_claims(batch):
    """(7) Every proposal carries the program-bound claim it was judged against."""
    for row in batch.get('hypotheses', ()):
        claim = row.get('claim')
        require(isinstance(claim, dict)
                and set(claim) == {'trigger', 'action_change', 'claim_id'},
                'sampled hypothesis has no well-formed claim')
        try:
            rebuilt = S.Claim(claim['trigger'], claim['action_change'])
        except (ValueError, TypeError) as exc:
            require(False, f'sampled claim is invalid: {exc}')
        require(claim['claim_id'] == rebuilt.claim_id,
                'claim id does not match the claim text it accompanies')
    claims = {}
    for row in batch.get('proposals', ()):
        candidate = row.get('edit', {}).get('candidate')
        if candidate:
            require(isinstance(row.get('claim'), dict),
                    'sampled proposal materialized a candidate without a claim')
            claims[candidate['candidate_id']] = row['claim']['claim_id']
    return claims


def audit_candidate(result, panel, sample_size, confidence, claim_id, base_key):
    """(1)-(4), (8) for one candidate's recorded validation."""
    require(tuple(int(t) for t in result.get('panel_task_ids', ())) == panel,
            'sampled candidate panel differs from the acceptance panel')
    require(result.get('base_skill_key') == base_key,
            'sampled base Skill differs from the batch head')
    require(result.get('claim_id') == claim_id,
            'sampled candidate was judged against a different claim')

    # (1) The executed subset is the one the recorded key selects.
    sample = tuple(int(t) for t in result.get('sample_task_ids', ()))
    require(sample == PPI.select_sample(panel, sample_size, result.get('sample_key', '')),
            'executed sample is not the one the recorded key selects')
    decision = result.get('decision') or {}
    require(int(decision.get('n_sample', -1))
            == PPI.effective_sample_size(len(panel), sample_size),
            'executed count is not the per-panel effective sample size')

    # (2) One row per panel task, measured exactly on the sample.
    rows = list(result.get('rows', ()))
    require([int(r['task_id']) for r in rows] == list(panel),
            'sampled per-task rows do not cover the panel in fixed order')
    require({int(r['task_id']) for r in rows if r.get('sampled')} == set(sample),
            'sampled rows disagree with the executed sample')

    # (3) The decision is recomputable from the rows.
    measured = {int(r['task_id']): float(r['measured_delta']) for r in rows if r['sampled']}
    predicted = {int(r['task_id']): float(r['delta_probability']) for r in rows}
    replay = PPI.estimate(predicted, measured, confidence=confidence)
    require(bool(decision.get('accepted')) == replay.accepted,
            'recorded sampled decision is not the one the rows imply')
    require(_close(decision.get('point'), replay.point)
            and _close(decision.get('lower'), replay.lower),
            'recorded sampled estimate does not match the recomputed one')

    enabled = result.get('verification_enabled')
    require(isinstance(enabled, bool),
            'sampled result does not record whether verification ran')
    for row in rows:
        # (8) Every prediction can be traced to its cached request.
        require(row.get('reviewer_cache_key'), 'Reviewer prediction has no request identity')
        # (4) The verifier reads every sampled task when enabled, and nothing else.
        verification = row.get('verification')
        require((verification is not None) == (enabled and row['sampled']),
                'verification does not cover exactly the sampled tasks')
        if verification is not None:
            require(verification.get('category') in CATEGORIES,
                    'verifier returned an unknown category')
            require(isinstance(verification.get('reason'), str)
                    and verification['reason'].strip(), 'verifier returned an empty reason')
    return 2 * len(sample), sum(1 for row in rows if not row['from_cache'])


def audit_memories(root, batch, config):
    """(5), (6) Both memories are exactly the views of rounds before this one.

    The Planner's memory is recomputed and compared character for character; its
    renderer reads no task, so equality is what rules out leaked val content.
    """
    changes = LED.read_changes(root, before_round=int(batch['round']))
    expected = ''
    if config['planner_memory_mode'] == 'aggregate':
        expected = MEM.PlannerMemory(
            changes, claims_verified=config['claim_verification'] == 'on').render()
    require(batch.get('planner_memory') == expected,
            'journaled Planner memory is not the aggregate the ledger yields')
    version = 0
    if config['reviewer_memory_mode'] == 'cases':
        version = len({change.candidate_id for change in changes if change.sampled})
    require(batch.get('reviewer_memory_version') == version,
            'journaled Reviewer memory does not cover exactly the earlier proposals')


def audit_batch(root, batch, config):
    """Replay a sampled batch from its journal alone."""
    claims = audit_claims(batch)
    audit_memories(root, batch, config)
    acceptance = batch.get('acceptance') or {}
    require(acceptance.get('mode') == 'sampled', 'sampled acceptance mode is missing')
    results = list(acceptance.get('candidates', ()))
    if not claims or not acceptance.get('task_ids'):
        require(not results, 'sampled acceptance scored a candidate it could not measure')
        return
    require({row.get('candidate_id') for row in results} == set(claims),
            'sampled acceptance does not cover exactly the proposed candidates')
    require(acceptance.get('scope') == 'val', 'sampled acceptance scope is missing')
    require(int(acceptance.get('sample_size', 0)) == int(config['acceptance_sample_size'])
            and float(acceptance.get('confidence', 0)) == float(config['acceptance_confidence']),
            'sampled acceptance parameters differ from the frozen protocol')
    panel = tuple(int(t) for t in acceptance.get('task_ids', ()))
    require(panel and tuple(sorted(panel)) == panel,
            'sampled acceptance panel is empty or not in fixed order')
    executions = requests = 0
    for row in results:
        spent = audit_candidate(row.get('result') or {}, panel,
                                int(acceptance['sample_size']), float(acceptance['confidence']),
                                claims[row['candidate_id']], batch['base_skill_key'])
        require((row.get('executions'), row.get('reviewer_requests')) == spent,
                'sampled candidate cost differs from its rows')
        executions += spent[0]
        requests += spent[1]
    require((acceptance.get('executions'), acceptance.get('predicted_requests'))
            == (executions, requests), 'sampled acceptance cost differs from its candidates')


def audit_summary(summary, journals, config):
    """The round summary reports the frozen switches and recomputable metrics."""
    for key in ('acceptance_sample_size', 'acceptance_confidence', 'claim_verification',
                'planner_memory_mode', 'reviewer_memory_mode'):
        require(summary.get(key) == config[key], f'summary {key} differs from protocol')
    require(summary.get('val_executions') == sum(
        int(b.get('acceptance', {}).get('executions', 0)) for b in journals),
        'sampled summary execution count mismatch')
    changes = [change for b in journals for change in LED.changes_from_journal(b)]
    require(summary.get('reviewer_metrics') == LED.reviewer_metrics(changes),
            'summary Reviewer metrics are not recomputable from the journals')
