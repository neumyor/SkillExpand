"""Record corrected raw gates without editing historical experiment evidence."""
import json
import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path('runs/tb21-raw-gate-reconciliation-20261008-1205'))
    parser.add_argument('--accept-qwen-input', action='store_true',
                        help='Record explicit user acceptance of the Qwen source despite retained exceptions')
    args = parser.parse_args()
    root = args.root
    reports = {name: json.loads((root / f'{name}.json').read_text())
               for name in ('qwen', 'deepseek')}
    invalid = {name: [row for row in report['rows'] if not row['valid']]
               for name, report in reports.items()}
    deepseek_passed = reports['deepseek']['coverage'] == '89 tasks x 3 attempts'
    qwen_passed = args.accept_qwen_input or reports['qwen']['coverage'] == '89 tasks x 3 attempts'
    if args.accept_qwen_input:
        assert reports['qwen']['tasks'] == 89 and reports['qwen']['trials'] == 267
        assert not reports['qwen']['missing_tasks'] and not reports['qwen']['incomplete_tasks']
    summary = {
        'status': 'complete' if deepseek_passed and qwen_passed else 'needs_attention',
        'reason': 'raw_input_gates_accepted' if deepseek_passed and qwen_passed else 'raw_input_gate_failed',
        'qwen_valid': reports['qwen']['valid_rollout_count'],
        'deepseek_valid': reports['deepseek']['valid_rollout_count'],
        'expected_per_source': 267, 'new_formal_requests': 0,
        'historical_runs_read_only': True, 'reward_zero_fabricated': False,
        'formal_resume_allowed': False,
        'formal_resume_reason': 'Input audit only; stage-specific reviewer budgets and completion audits remain required',
        'input_gate_passed': {'E3': deepseek_passed, 'E4': qwen_passed,
                              'E5': deepseek_passed and qwen_passed},
        'qwen_acceptance_override': args.accept_qwen_input,
        'qwen_acceptance_authorization': 'user: qwen视为通过。' if args.accept_qwen_input else None,
        'timeout_policy': 'user-authorized timeout outcomes valid; rewards unchanged',
        'source_gates': {name: report['coverage'] for name, report in reports.items()},
        'accepted_timeouts': {name: report.get('accepted_timeout_count', 0) for name, report in reports.items()},
        'affected_stages': ['E3', 'E4', 'E5'],
        'E1_empirical_independent': True,
        'next_step': ('Continue stage-specific work with unchanged reviewer budgets; retain source exceptions'
                      if deepseek_passed and qwen_passed else
                      'retry only invalid original slots with matched source configuration; preserve trial mapping and old evidence'),
    }
    for name, value in [('status.json', summary), ('audit.json', summary),
                        ('result.json', summary), ('retry_slots.json', invalid),
                        ('manifest.json', {'sources': {k: v['source_root'] for k, v in reports.items()},
                         'validator': 'scripts/validate_tb21_rollouts.py',
                         'focused_tests': '11 passed', 'scope': '89-task closed-set input audit'})]:
        (root / name).write_text(json.dumps(value, indent=2) + '\n')
    (root / 'report.md').write_text(
        '# Corrected TB2.1 raw input gate\n\n'
        'The user explicitly authorized timeout outcomes as valid raw rollouts. Rewards are unchanged. '
        'Other exceptions and missing ordinary verifier rewards remain excluded.\n\n'
        + '\n'.join(f"{name}: {report['valid_rollout_count']}/267 valid, "
            f"{report.get('accepted_timeout_count', 0)} accepted timeouts; "
            f"excluded: {report['exception_or_metadata_by_reason']}"
            for name, report in reports.items()) + '\n\n'
        'E3 DeepSeek input passes when all 267 outcomes are valid under this policy. '
        + ('Qwen input is explicitly accepted by the user for E4/E5; its four exception records remain unchanged. '
           'No source retry is required for this accepted input gate. '
           if args.accept_qwen_input else 'E4/E5 Qwen input still requires its own complete gate. ') +
        'Input acceptance does not complete a stage or replenish reviewer correction budgets. '
        'E1 frozen-library empirical evaluation continues independently; no complete baseline comparison is claimed.\n')
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
