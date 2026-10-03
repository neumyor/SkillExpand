"""Independent resume, reviewer, and frozen-plan checks before full launch."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess


def evidence_hashes(run):
    paths = set()
    for pattern in ('discovery/results/*.json', 'discovery/trials/*.json',
                    'evolution/round-*/cards/*.json', 'evolution/round-*/trials/*.json',
                    'l2_proposals/*/*.json', 'l2_patterns/*.json', '**/*.requests.jsonl'):
        paths.update(run.glob(pattern))
    return {str(path.relative_to(run)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    root = parser.parse_args().root.resolve()
    campaign = runpy.run_path(str(root / 'code/run_campaign.py'))
    manifest = campaign['verify'](root)
    os.environ.update(campaign['environment'](root))
    from omegaconf import OmegaConf
    from skillexpand import schema as S
    from skillexpand.runtime import agent_factory as F
    from skillexpand.l2 import card_review as CR

    checks = {}
    for benchmark in campaign['BENCHMARKS']:
        run = root / 'preflight' / benchmark / 'run'
        before = evidence_hashes(run)
        with (root / 'preflight' / f'{benchmark}-resume-check.log').open('ab') as log:
            subprocess.run(campaign['command'](root, '_stage', 'preflight', benchmark,
                                               'evolve-2', 999), cwd=manifest['repo'],
                           env=campaign['environment'](root), stdout=log,
                           stderr=subprocess.STDOUT, check=True)
        if evidence_hashes(run) != before:
            raise ValueError(f'{benchmark}: replay changed evidence or issued requests')

        cfg = OmegaConf.load(run / 'config.json')
        base = S.from_dict(S.Skill, json.loads((run / 'initial_skills.json').read_text())[0])
        exp = S.from_dict(S.TaskExperience,
                          json.loads((run / 'evolution/round-2/cards/0.json').read_text()))
        candidates = [
            {'id': 'C1', 'body': base.body + '\nCheck the observation before acting.'},
            {'id': 'C2', 'body': base.body + '\nKeep the final response concise.'},
            {'id': 'C3', 'body': base.body + '\nUse the observed feedback to check progress.'},
        ]
        card = CR.card_payload([exp])[0]
        host = F.build_reasoning_host(
            cfg,
            root / 'preflight/reviewer-probe' / f'{benchmark}.usage.json',
            role='l2_reviewer',
        )
        reviewer = CR.CardReviewer(host)
        probe = root / 'preflight/reviewer-probe' / f'{benchmark}-response-v2.json'
        if probe.exists():
            raw = json.loads(probe.read_text())['raw']
        else:
            raw = reviewer.review(base, candidates, card)
            campaign['save'](probe, {'raw': raw})
        corrected = False
        for attempt in range(3):
            try:
                judgments = CR.parse_card_review(raw, base, candidates, card,
                                                 allow_legacy=False)
                break
            except (ValueError, KeyError, TypeError) as exc:
                if attempt == 2:
                    raise
                correction = {'error': str(exc),
                    'instruction': ('Return exactly {"candidates":[{"id":"C1",'
                        '"evidence_ids":[],"rule_ids":[],"reason":"...",'
                        '"old_outcome":"failure","new_outcome":"unknown"}, ...]} '
                        'with C1, C2, C3 once each. The top-level keys must NOT be '
                        'C1/C2/C3. Use only supplied IDs; do not use placeholders such '
                        'as etc. Copy card.current_observed_outcome into old_outcome '
                        'when known; otherwise infer CURRENT separately or use unknown. Do not '
                        'return label or effect; the program derives the relative effect. '
                        'For a directional outcome difference provide at least one card '
                        'evidence ID and one changed rule ID.')}
                repair = probe.with_name(probe.stem + f'-repair-{attempt + 1}.json')
                if repair.exists():
                    raw = json.loads(repair.read_text())['raw']
                else:
                    raw = reviewer.review(base, candidates, card, correction=correction)
                    campaign['save'](repair, {'raw': raw, 'correction': correction})
                corrected = True
        if len(judgments) != len(candidates):
            raise ValueError(f'{benchmark}: reviewer omitted a candidate')

        output = subprocess.check_output(
            [manifest['python'], '-m', 'skillexpand',
             *campaign['stage_args'](root, 'full', benchmark, 'cold-start'), '--show-plan'],
            cwd=manifest['repo'], env=campaign['environment'](root), text=True)
        plan = json.loads(output)
        if plan['counts'] != manifest['benchmarks'][benchmark]['counts']:
            raise ValueError(f'{benchmark}: full split differs from registered inputs')
        if (root / 'full' / benchmark / 'run').exists():
            raise ValueError(f'{benchmark}: read-only plan created a run directory')
        checks[benchmark] = {'resume_unchanged': True, 'evidence_files': len(before),
                             'reviewer_candidates': len(judgments),
                             'reviewer_format_correction': corrected,
                             'full_plan': plan}

    campaign['save'](root / 'preflight/independent-checks.json',
                     {'status': 'passed',
                      'manifest_hash': campaign['digest'](root / 'manifest.json'),
                      'checks': checks})
    print(json.dumps(checks))


if __name__ == '__main__':
    main()
