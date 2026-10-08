"""Offline integrity audit of v5 checkpoints; never calls a model or environment."""
import argparse
import json
from pathlib import Path

from skillexpand.l1 import learning as L, protocol as P


# These errors are emitted by the provider callback for an individual failed
# attempt before GPTWrapper retries the same logical request. They do not make
# a completed checkpoint invalid; abandoned requests and unknown errors do.
TRANSIENT_PROVIDER_ERRORS = frozenset({
    'Timeout', 'TimeoutError', 'APIError', 'APIConnectionError',
    'RateLimitError', 'ServiceUnavailableError', 'ConnectionError',
    'RemoteDisconnected',
})


def require(condition, message):
    if not condition:
        raise ValueError(message)


def audit_checkpoint(data, adapter):
    """Reconstruct factual evidence from events, not from the stored verdict."""
    require(data['protocol'] == P.VERSION, 'Unsupported L1 protocol')
    trials = data['trials']
    require([t['index'] for t in trials] == list(range(1, len(trials)+1)),
            'Duplicate or missing trial indices')
    completed = [t for t in trials if t['status'] == 'completed']
    require(completed and trials[-1]['status'] == 'completed', 'Incomplete execution')
    for i, t in enumerate(trials):
        require(t['status'] in ('completed', 'interrupted'), 'Unfinished trial')
        require(not t['success'] or t['status'] == 'completed', 'Interrupted success')
        require(not t['success'] or i == len(trials)-1, 'Execution continued after success')
        refs = [e['ref'] for e in t['events']]
        require(len(refs) == len(set(refs)), 'Duplicate event references')
        require(all(not e.get('blocked_action') for e in t['events'] if 'observation' in e),
                'Blocked action has an observation')
        observations = [e for e in t['events'] if 'observation' in e]
        if t['status'] == 'completed':
            require(all('environment' in e for e in observations), 'Missing environment outcomes')
            require(t['success'] == (observations[-1]['environment']['success'] if observations else False),
                    'Trial outcome differs from environment result')
    exp = data['experience']
    identity = data.get('identity')
    if identity is not None:
        from skillexpand import schema as S
        require(S.content_hash(identity) == data['signature'], 'Checkpoint identity hash mismatch')
        require(exp.get('evolution_round', 0) == identity.get('evolution_round', 0), 'Round identity mismatch')
        require(exp['initial_skill_key'] == (identity['skill']['key'] if identity['skill'] else None),
                'Injected Skill identity mismatch')
        require(sum(t['phase'] == 'autonomous' for t in completed) <= identity['k'], 'Autonomous budget exceeded')
        require(sum(t['phase'] == 'supervised' for t in completed) <= int(identity['supervised']),
                'Supervised budget exceeded')
    require(exp['trial_rewards'] == [t['success'] for t in completed], 'Trial reward mismatch')
    require(exp['trial_phases'] == [t['phase'] for t in completed], 'Trial phase mismatch')
    require(exp['num_trials'] == len(completed), 'Trial count mismatch')
    solved = any(t['success'] for t in completed)
    require(exp['reward'] == solved, 'Task reward mismatch')
    require(exp['failed_trajectories'] == [t['trajectory'] for t in completed if not t['success']],
            'Failed trajectory mismatch')
    require(exp['final_trajectory'] == (completed[-1]['trajectory'] if solved else None),
            'Final trajectory mismatch')
    synthesis = data['synthesis']
    payload = synthesis['input']
    available = P.evidence(exp['task'], completed, adapter=adapter)
    require(payload['hypotheses'] == {}, 'Retry hypotheses leaked into final synthesis')
    require(payload['attempts'] == [{k: t.get(k) for k in
            ('index', 'phase', 'status', 'success', 'termination')} for t in completed],
            'Synthesis attempt ledger mismatch')
    require(payload['evidence'].get('task') == available['task'], 'Synthesis task mismatch')
    for ref, item in payload['evidence'].items():
        require(available.get(ref) == item, f'Evidence differs from actual events: {ref}')
    require(payload['omitted_evidence'] == len(available)-len(payload['evidence']),
            'Evidence omission count mismatch')
    latest = [ref for ref in available if ref.startswith(f"t{completed[-1]['index']}:")]
    require(not latest or latest[-1] in payload['evidence'], 'Final observation was omitted')
    initial = L.parse(synthesis['raw'], payload['evidence'], solved)
    require(initial == synthesis['initial_result'], 'Initial extraction differs from raw output')
    if initial['rejected'] and len(initial['claims']) < 2:
        require('repair' in synthesis, 'Rejected claims lack a repair attempt')
        repair = synthesis['repair']
        expected_input = L.repair_input(payload, initial)
        require(repair['input'] == expected_input, 'Repair input differs from rejected claims')
        repaired = L.parse(repair['raw'], payload['evidence'], solved,
                           max_claims=expected_input['open_slots'])
        require(repair['parsed'] == repaired, 'Repair parse differs from raw output')
        parsed = L.finish(initial, repaired)
    else:
        require('repair' not in synthesis, 'Unneeded extraction repair')
        parsed = L.finish(initial)
    require(parsed == synthesis['result'], 'Stored synthesis differs from extraction records')
    card = exp['experience_card']
    expected = L.card(exp['task_id'], exp['task'], trials, parsed,
                      None, card['audit_id'], len,
                      evidence=available, card_id=exp['experience_id'],
                      benchmark=exp['benchmark'], family_id=exp['family_id'],
                      evolution_round=exp.get('evolution_round', 0),
                      skill_key=exp['initial_skill_key'])
    # Token counts depend on the actor tokenizer; compare every semantic field.
    counters = {'token_count', 'over_target', 'over_budget'}
    require({k: v for k, v in card.items() if k not in counters} ==
            {k: v for k, v in expected.items() if k not in counters}, 'Card differs from evidence')
    return dict(task_id=exp['task_id'], benchmark=exp['benchmark'], success=solved,
                trials=len(completed), interrupted=len(trials)-len(completed),
        extraction_status=parsed['status'], extraction_outcome=parsed['outcome'],
                claims=len(card['claims']),
                omitted_evidence=payload['omitted_evidence'])


def audit_usage(checkpoint, data=None):
    path = Path(checkpoint).with_suffix('.usage.json')
    usage = json.loads(path.read_text())
    rows = [json.loads(line) for line in path.with_suffix('.requests.jsonl').read_text().splitlines()]
    pending, finished = set(), set()
    starts = ends = errors = transient_errors = nonretryable_errors = abandoned = 0
    tokens = dict(prompt_tokens=0, completion_tokens=0, total_tokens=0)
    for row in rows:
        rid = row['run_id']
        if row['event'] == 'start':
            require(rid and rid not in pending | finished, 'Duplicate request start')
            pending.add(rid)
            starts += 1
        else:
            require(row['event'] in ('end', 'error', 'abandoned') and rid in pending, 'Unmatched request end')
            pending.remove(rid)
            finished.add(rid)
            if row['event'] in ('error', 'abandoned'):
                errors += 1
                if row['event'] == 'abandoned':
                    abandoned += 1
                elif row.get('error_type') in TRANSIENT_PROVIDER_ERRORS:
                    transient_errors += 1
                else:
                    nonretryable_errors += 1
            else:
                ends += 1
                reported = (row.get('provider') or {}).get('token_usage')
                require(reported is not None, 'Provider token usage missing; cannot verify totals')
                for key in tokens:
                    tokens[key] += reported[key]
    require(not pending, 'In-flight requests remain; audit incomplete')
    for key, value in dict(started_requests=starts, successful_requests=ends,
                           failed_requests=errors, **tokens).items():
        require(usage[key] == value, f'Usage mismatch: {key}')
    if data is not None:
        synthesis = data['synthesis']
        def verify_response(payload, raw, stage):
            serialized = json.dumps(payload, ensure_ascii=False)
            # The payload is the final Human message. Rejected model output in
            # a repair request can itself contain the entire original payload;
            # a substring search would misidentify that nested copy as a request.
            matching = [r['run_id'] for r in rows if r['event'] == 'start' and
                        any(prompt == serialized or prompt.endswith('\nHuman: ' + serialized)
                            for prompt in r['prompts'])]
            require(matching, f'No logged request contains the {stage} input')
            responses = [r for r in rows if r['run_id'] in matching and r['event'] == 'end']
            require(len(responses) == 1 and any(
                g['text'].strip() == raw for group in responses[0]['generations']
                for g in group), f'{stage} raw output differs from logged model response')
        verify_response(synthesis['input'], synthesis['raw'], 'extraction')
        if 'repair' in synthesis:
            verify_response(synthesis['repair']['input'], synthesis['repair']['raw'],
                            'extraction repair')
    return dict(
        requests=starts,
        failed_requests=errors,
        transient_errors=transient_errors,
        nonretryable_errors=nonretryable_errors,
        abandoned_requests=abandoned,
        # Token totals remain incomplete whenever a provider attempt failed.
        tokens_complete=errors == 0,
        # Checkpoint integrity is still auditable when those failures were
        # transient attempts followed by a successful retry.
        audit_complete=(not pending and abandoned == 0 and nonretryable_errors == 0),
        **tokens,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('checkpoints', nargs='+', type=Path)
    parser.add_argument('--config', help='Config with custom adapter/overrides; default: benchmark config')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    from omegaconf import OmegaConf
    from skillexpand.runtime.agent_factory import load_config
    from skillexpand.l1.adapters import resolve
    from skillexpand.l1.runner import save
    rows = []
    for path in args.checkpoints:
        try:
            data = json.loads(path.read_text())
            cfg = OmegaConf.load(args.config) if args.config else load_config(data['experience']['benchmark'])
            result = audit_checkpoint(data, resolve(cfg))
            result['usage'] = audit_usage(path, data)
            rows.append(dict(path=str(path), integrity='passed', **result))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            rows.append(dict(path=str(path), integrity='failed', error=str(exc)))
    passed = all(r['integrity'] == 'passed' for r in rows)
    save(args.output, dict(integrity_passed=passed, units=rows,
         scope='Saved-record consistency, not semantic truth or benchmark performance.'))
    print(json.dumps(dict(integrity_passed=passed, units=len(rows), output=str(args.output))))
    raise SystemExit(0 if passed else 1)


if __name__ == '__main__':
    main()
