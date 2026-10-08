"""Prepare, probe, and resume only the existing SearchQA Reviewer repair run."""

import argparse
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import time


FILES = (
    'evaluation/validation.py', 'l2/audit.py', 'l2/loop.py',
    'l2/reviewer_coevolution.py', 'runtime/reviewer_retry.py',
)


def evidence_hashes(run, digest):
    patterns = (
        'discovery/results/*.json', 'discovery/trials/*',
        'evolution/round-*/cards/*.json', 'evolution/round-*/trials/*',
        'evolution/round-*/input.json', 'evolution/round-*/manifest.json',
        'evolution/round-*/batches.json', 'l2_batches/*.json', 'l2_proposals/*/*.json',
        'l2_patterns/*.json', '**/*.requests.jsonl', 'reviewer_feedback/*.jsonl',
        'reviewer_feedback.jsonl', 'skills.jsonl', 'config.json', 'split.json',
    )
    paths = {p for pattern in patterns for p in run.glob(pattern)
             if p.is_file() and p.suffix != '.lock'}
    return {str(p.relative_to(run)): digest(p) for p in sorted(paths)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'refresh', 'probe', 'launch', 'check'))
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    repo = Path(__file__).resolve().parents[1]
    campaign = runpy.run_path(str(repo / 'scripts/run_campaign.py'))
    read, save, digest = (campaign[key] for key in ('read', 'save', 'digest'))
    run = root / 'full/searchqa/run'
    repair = root / 'repairs/reviewer-update-recovery'
    report_path = repair / 'repair.json'
    os.environ.setdefault('EXPE_REVIEWER_ATTEMPTS', '32')
    os.environ.setdefault('EXPE_STAGE_ATTEMPTS', '0')

    if args.action in ('prepare', 'refresh'):
        if args.action == 'prepare' and report_path.exists():
            raise ValueError('Repair already prepared; use probe or launch')
        if args.action == 'refresh' and not report_path.exists():
            raise ValueError('Prepare repair before refreshing its code snapshot')
        with campaign['locked'](root / 'full/searchqa/job.lock'), campaign['locked'](run / 'campaign.lock'):
            manifest = read(root / 'manifest.json')
            if Path(manifest['repo']).resolve() != repo or not (root / 'repair.json').exists():
                raise ValueError('Only an existing repair campaign in this repository is supported')
            before = evidence_hashes(run, digest)
            if args.action == 'refresh' and before != read(report_path)['preserved_evidence']:
                raise ValueError('Evidence changed since initial preparation')
            report = {'started': time.time(), 'old_manifest_hash': digest(root / 'manifest.json'),
                      'old_l2_manifest_hash': digest(run / 'l2_manifest.json'),
                      'preserved_evidence': before,
                      'recovery': {'reviewer_attempts': 32, 'stage_max_attempts': 0},
                      'scope': 'SearchQA evolve-2 boundary only; original run and ALFWorld untouched'}
            repair.mkdir(parents=True, exist_ok=True)
            backup_root = repair
            if args.action == 'refresh':
                backup_root = repair / 'revisions' / str(time.time_ns())
                backup_root.mkdir(parents=True)
                shutil.copyfile(report_path, backup_root / 'repair.before.json')
            shutil.copyfile(root / 'manifest.json', backup_root / 'manifest.before.json')
            shutil.copyfile(run / 'l2_manifest.json', backup_root / 'l2_manifest.before.json')
            shutil.copyfile(root / 'full/searchqa/status.json', backup_root / 'status.before.json')
            for relative in (*('code/src/skillexpand/' + name for name in FILES), 'code/run_campaign.py'):
                destination = root / relative
                source = repo / ('scripts/run_campaign.py' if relative == 'code/run_campaign.py'
                                 else 'src/skillexpand/' + relative.split('code/src/skillexpand/')[1])
                if destination.exists():
                    backup = backup_root / 'code.before' / relative
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(destination, backup)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                manifest['files'][relative] = digest(destination)
            manifest['git'] = campaign['git_identity'](repo)
            save(root / 'manifest.json', manifest)
            from skillexpand import schema as S
            protocol = read(run / 'l2_manifest.json')
            protocol['code'] = {str(p.relative_to(root / 'code/src/skillexpand')): S.content_hash(p.read_text())
                                for p in sorted((root / 'code/src/skillexpand').rglob('*.py'))}
            save(run / 'l2_manifest.json', protocol)
            report.update(new_manifest_hash=digest(root / 'manifest.json'),
                          new_l2_manifest_hash=digest(run / 'l2_manifest.json'))
            if evidence_hashes(run, digest) != before:
                raise ValueError('Preparation changed preserved evidence')
            save(report_path, report)
        print('Repair snapshot prepared; preserved evidence unchanged.')
        return

    frozen = runpy.run_path(str(root / 'code/run_campaign.py'))
    manifest = frozen['verify'](root)
    report = read(report_path)
    if report['new_manifest_hash'] != digest(root / 'manifest.json'):
        raise ValueError('Repair manifest changed after preparation')
    os.environ.update(frozen['environment'](root))
    sys.path[:0] = os.environ['PYTHONPATH'].split(os.pathsep)

    if args.action == 'probe':
        # health() issues real generation requests for the frozen role models.
        health = frozen['health'](root)
        from omegaconf import OmegaConf
        from skillexpand import schema as S
        from skillexpand.l2 import reviewer_coevolution as RC
        from skillexpand.l2.audit import audit_round
        from skillexpand.runtime import agent_factory as F
        from skillexpand.evaluation.validation import PredictedSkillScorer
        from skillexpand.persistence.artifacts import load_cold_start
        cfg = OmegaConf.load(run / 'config.json')
        rows = tuple(S.from_dict(RC.PairedFeedback, row)
                     for row in RC.read_jsonl(run / 'reviewer_feedback.jsonl'))
        round1 = tuple(row for row in rows if row.round_index == 1)
        round2 = tuple(row for row in rows if row.round_index == 2)
        rule_update = RC.generate_reviewer_update(
            round1, generation_round=1,
            host_factory=lambda: F.build_reasoning_host(cfg, repair / 'rule-probe.usage.json',
                                                        role='l2_reviewer'),
        )
        def forbidden():
            raise AssertionError('Empty observations attempted a model request')
        empty_update = RC.generate_reviewer_update(round2, generation_round=2, parent_version=1,
                                                   host_factory=forbidden)
        base = S.from_dict(S.Skill, read(run / 'initial_skills.json')[0])
        routes = RC.FixedTrainRoutes.from_plan(load_cold_start(run)[1])
        scorer = PredictedSkillScorer(cfg, routes, None)
        host = F.build_reasoning_host(cfg, repair / 'prediction-probe.usage.json', role='l2_reviewer')
        prediction, attempts = scorer._review(host, scorer.prompt(F.task_text_of(cfg, 0), base))
        checks = {'status': 'passed', 'time': time.time(), 'health': health,
                  'manifest_hash': digest(root / 'manifest.json'),
                  'rule_update': S.to_dict(rule_update), 'empty_update': S.to_dict(empty_update),
                  'prediction': prediction, 'format_attempts': attempts,
                  'cold_start_audit': frozen['audit_stage'](root, 'full', 'searchqa', 'cold-start'),
                  'round1_audit': audit_round(run, 1), 'round2_artifact_audit': audit_round(run, 2)}
        if evidence_hashes(run, digest) != report['preserved_evidence']:
            raise ValueError('Smoke checks changed preserved evidence')
        save(repair / 'checks.json', checks)
        print('Real provider, prediction, rule update, empty evidence, and offline audits passed.')
    elif args.action == 'launch':
        checks = read(repair / 'checks.json')
        if checks['status'] != 'passed' or checks['manifest_hash'] != digest(root / 'manifest.json'):
            raise ValueError('Matching repair checks required')
        if evidence_hashes(run, digest) != report['preserved_evidence']:
            raise ValueError('Evidence changed since checks')
        with frozen['locked'](root / 'full/searchqa/job.lock'), frozen['locked'](run / 'campaign.lock'):
            log = root / 'full/searchqa/resume-job.log'
            with log.open('ab') as stream:
                child = subprocess.Popen(frozen['command'](root, '_job', 'full', 'searchqa'),
                    cwd=manifest['repo'], env=frozen['environment'](root), stdin=subprocess.DEVNULL,
                    stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            save(root / 'full/searchqa/resume.pid', child.pid)
            save(repair / 'launch.json', {'pid': child.pid, 'time': time.time(),
                                        'reviewer_attempts': os.environ['EXPE_REVIEWER_ATTEMPTS'],
                                        'stage_max_attempts': os.environ['EXPE_STAGE_ATTEMPTS']})
        print(f'SearchQA-only detached job launched: {child.pid}')
    else:
        actual = evidence_hashes(run, digest)
        changed = [path for path, value in report['preserved_evidence'].items()
                   if actual.get(path) != value]
        if changed:
            raise ValueError(f'Preserved evidence changed: {changed[:3]}')
        print('All preserved evidence hashes match.')


if __name__ == '__main__':
    main()
