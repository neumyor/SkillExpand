"""Recover only the two frozen E5 task-28 reviewer identities."""
import json
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

from omegaconf import OmegaConf
from skillexpand import schema as S
from skillexpand.evaluation.validation import PredictedSkillScorer, ScoreCache
from skillexpand.runtime.llm_relay import TencentSandboxTransport, iter_sse_events


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def main():
    api_key = os.environ['OPENAI_API_KEY']
    root = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand/runs')
    source = root / 'tb21-e5-qwen-deepseek-mixed-parallel-20261006-recovery4-task28-noschema'
    proposals = root / 'tb21-e5-qwen-deepseek-mixed-parallel-20261006-recovery3-candidate7/l2_proposals'
    audit = root / 'tb21-e5-20261006-recovery5-audit'
    out = root / 'tb21-e5-20261006-recovery7-task28-stream'
    pre = json.loads((source / 'recovery_preflight.json').read_text())
    old_status = json.loads((source / 'status.json').read_text())
    assert old_status['provider_requests_started'] == 0
    cfg = OmegaConf.create(json.loads((source / 'config.json').read_text()))
    base = S.from_dict(S.Skill, json.loads((source / 'initial_skills.json').read_text())[0])
    matches = []
    for path in proposals.glob('*/candidate-*.json'):
        value = json.loads(path.read_text()).get('candidate')
        if value and S.content_hash(value['skill']['body']) == pre['candidate_body_hash']:
            matches.append((path, S.from_dict(S.CandidateSkill, value)))
    assert matches and len({item[1].candidate_id for item in matches}) == 1
    candidate_path, candidate = matches[0]
    assert S.content_hash(base.body) == pre['base_body_hash']
    route_manifest = json.loads((source / 'routes/train/manifest.json').read_text())
    routes = SimpleNamespace(fingerprint=S.content_hash(route_manifest), groups={base.skill_id: tuple(range(89))})
    scorer = PredictedSkillScorer(cfg, routes, None, workers=1)
    assert scorer.protocol_hash == pre['protocol_hash'], (scorer.protocol_hash, pre['protocol_hash'])
    rows = [json.loads(line) for line in (audit / 'predicted_scores.reconciled.jsonl').read_text().splitlines()]
    assert len(rows) == len({r['cache_key'] for r in rows}) == 176
    for skill in (base, candidate.skill):
        for task in range(89):
            key = ScoreCache.make_key('terminalbench', pre['panel_key'], task, 'predicted:' + scorer.protocol_hash, skill.body)
            hits = [r for r in rows if r['cache_key'] == key]
            assert len(hits) == (0 if task == 28 else 1)
            if hits:
                scorer._parse(json.dumps({k: hits[0][k] for k in ('probability_true', 'predicted_success', 'reason')}))
    out.mkdir(exist_ok=False)
    for name in ('config.json', 'manifest.json', 'l2_manifest.json', 'initial_skills.json', 'skills.jsonl', 'split.json', 'cold_start_complete.json'):
        shutil.copy2(source / name, out / name)
    shutil.copytree(source / 'routes', out / 'routes')
    (out / 'train').mkdir()
    shutil.copy2(audit / 'predicted_scores.reconciled.jsonl', out / 'train/predicted_scores.jsonl')
    scorer.cache = ScoreCache(out / 'train/predicted_scores.jsonl')
    ledger = {'source_run': str(source), 'candidate_source': str(candidate_path), 'old_runs_read_only': True,
              'reused_records': 176, 'missing_task_ids': [28], 'requests': [], 'retry_budget_increased': False,
              'recovery4_formal_requests_started': 0, 'max_outer_attempts': pre['retry_policy']['max_outer_attempts'],
              'requests_this_run_per_arm': 1, 'canary_source': str(root / 'tb21-e5-hello-diagnostics-20261006-214322')}
    save(out / 'recovery_ledger.json', ledger)
    save(out / 'status.json', {'stage': 'E5', 'status': 'running', 'pid': os.getpid(), 'missing_task_ids': [28]})
    (out / 'PID').write_text(str(os.getpid()) + '\n')
    transport = None
    try:
        transport = TencentSandboxTransport(template='sdt-28lb4d9k', api_key=api_key,
                    api_base=cfg.benchmark.rollout.provider_base_url, metadata={'x-mounts': '[]'}, request_timeout=300)
        for arm, skill in (('base', base), ('candidate', candidate.skill)):
            prompt = scorer.prompt(route_manifest['tasks']['28'], skill)
            (out / (arm + '.prompt.txt')).write_text(prompt)
            timeline = {'arm': arm, 'task_id': 28, 'started_at': time.time(), 'chunks': 0,
                        'model': cfg.models.l2_reviewer, 'stream': True, 'enable_thinking': True,
                        'response_format': 'omitted', 'skill_body_hash': S.content_hash(skill.body)}
            ledger['requests'].append(timeline)
            save(out / 'recovery_ledger.json', ledger)
            content = []
            def chunks():
                for chunk in transport.stream({'model': cfg.models.l2_reviewer, 'stream': True,
                        'messages': [{'role': 'user', 'content': prompt}], 'enable_thinking': True}):
                    timeline['chunks'] += 1
                    timeline.setdefault('first_chunk_at', time.time())
                    with (out / (arm + '.sse')).open('ab') as stream:
                        stream.write(chunk.encode() if isinstance(chunk, str) else chunk)
                    yield chunk
            for event in iter_sse_events(chunks()):
                choice = (event.get('choices') or [{}])[0]
                delta = choice.get('delta') or {}
                if delta.get('content'):
                    timeline.setdefault('first_content_at', time.time())
                    content.append(delta['content'])
                if choice.get('finish_reason'):
                    timeline['finish_reason'] = choice['finish_reason']
            raw = ''.join(content)
            (out / (arm + '.response.txt')).write_text(raw)
            parsed = scorer._parse(raw)
            key = ScoreCache.make_key('terminalbench', pre['panel_key'], 28, 'predicted:' + scorer.protocol_hash, skill.body)
            record = {'task_id': 28, 'skill_key': skill.key, 'cache_key': key, 'panel_key': pre['panel_key'],
                      'protocol_hash': scorer.protocol_hash, 'format_attempts': 1, 'response_format': 'omitted',
                      'result_path': str(out / (arm + '.response.txt')), **parsed}
            assert scorer.cache.put(key, record)
            timeline.update(status='ok', ended_at=time.time(), cache_key=key)
            save(out / 'recovery_ledger.json', ledger)
        validation = scorer.validate(base.skill_id, base, candidate.skill, tuple(range(89)), pre['panel_key'])
        save(out / 'validation.json', S.to_dict(validation))
        save(out / 'summary.json', {'metrics': validation.metrics, 'candidate_gate_passed': validation.passed,
                                   'scientific_scope': '89-task closed-set train/evolution acceptance panel'})
        save(out / 'audit.json', {'reviewer_coverage': {'base': 89, 'candidate': 89}, 'cache_records': len(scorer.cache),
                                'five_field_identity_verified': True, 'candidate_gate_executed': True,
                                'candidate_committed': False, 'remaining': 'Original L2 acceptance/commit and stage audit'})
        save(out / 'status.json', {'stage': 'E5', 'status': 'needs_attention', 'reviewer_recovery': 'complete',
                                  'reason': 'Reviewer coverage restored; original L2 acceptance/commit and final audit pending',
                                  'candidate_gate_passed': validation.passed})
        print(json.dumps({'out': str(out), 'metrics': validation.metrics, 'gate_passed': validation.passed}), flush=True)
    except Exception as exc:
        if ledger['requests'] and ledger['requests'][-1].get('status') != 'ok':
            ledger['requests'][-1].update(status='error', error_class=getattr(exc, 'error_class', type(exc).__name__), error=str(exc), ended_at=time.time())
        save(out / 'recovery_ledger.json', ledger)
        save(out / 'status.json', {'stage': 'E5', 'status': 'needs_attention', 'error_class': getattr(exc, 'error_class', type(exc).__name__), 'error': str(exc)})
        raise
    finally:
        if transport is not None:
            transport.close()


if __name__ == '__main__':
    main()
