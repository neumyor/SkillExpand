"""Build a concise report from this root's independent artifact audit."""
import argparse
import json
from pathlib import Path

from audit_results import ROOT, build_audit


def display(value):
    return 'unknown' if value is None else str(value)


def token_line(label, values):
    parts = [f"{name}={display(row['observed_total'])} observed ({row['unknown_records']} unknown records)"
             for name, row in values.items()]
    return label + ': ' + '; '.join(parts) + '.'


def render(audit):
    lines = ['# Generic Skill Evolution Result', '',
             'Status: ' + ('accepted' if audit['accepted'] else 'not accepted') + '.',
             f"Task: `{audit['task_name']}`. Executor: `{audit['model_name']}`.",
             'The historical targeted 5/5 result receives no acceptance credit.', '',
             'Acceptance requires a new complete batch of five official successes, exact audited bindings, '
             'matching frozen execution settings and full mounted Skill resources, and five fresh unique sandboxes.',
             'Generation uses the native generic `SkillEditor.propose_package`; module generality is a '
             'human preaccept audit requirement, and passing execution on this task does not establish transfer.', '',
             token_line('Generation tokens', audit['generation_tokens']),
             token_line('Executor tokens', audit['executor_tokens']), '',
             '| Candidate | Generated |', '| --- | --- |']
    for row in audit['candidates']:
        lines.append(f"| `{row['path']}` | {row['generated']} |")
    lines += ['',
             '| Generation Attempt | Status | Seconds | Error |', '| --- | --- | --- | --- |']
    for row in audit['generation_attempts']:
        error = (row.get('error') or {}).get('error_type') or row.get('outcome_reason')
        lines.append(f"| `{row['path']}` | {row['status']} | {display(row['seconds'])} | {display(error)} |")
    lines += ['', '| Trial | Valid Artifacts | Reward | Fresh Sandbox | Seconds | Problems |',
              '| --- | --- | --- | --- | --- | --- |']
    for row in audit['trials']:
        problems = row['problems'] + [str(row[key]) for key in ('error_type', 'exception_type') if row.get(key)]
        lines.append(f"| `{row['path']}` | {row['artifact_valid']} | {display(row['reward'])} | "
                     f"{row['fresh_unique_sandbox']} | {display(row.get('duration_seconds'))} | "
                     + ', '.join(problems) + ' |')
    lines += ['', '| Acceptance Batch | Passed | Valid | Successful | Problems |', '| --- | --- | --- | --- | --- |']
    for row in audit['acceptance_batches']:
        problems = row['problems'] + row.get('preaccept', {}).get('problems', [])
        lines.append(f"| `{row['path']}` | {row['passed']} | {row.get('valid_trials', 0)} | "
                     f"{row.get('successful_trials', 0)} | " + ', '.join(problems) + ' |')
    lines += ['', 'All candidate directories, generation attempts, execution failures and incomplete trials are '
              'retained in `artifact_audit.json`. Missing token measurements remain unknown; observed totals '
              'must not be treated as complete totals when records are missing.', '']
    lines += ['Deliverables: `generic_editor.diff`, `final_skill/SKILL.md`, `reproduce.md`, '
              '`genericity_audit.md`, `candidate_002_review.md` and precise requests under '
              '`development/candidate-*/generation_attempts/`.', '']
    if (ROOT / 'research_notes.md').exists():
        lines += [(ROOT / 'research_notes.md').read_text(), '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'report.md')
    args = parser.parse_args()
    audit = build_audit()
    (ROOT / 'artifact_audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    args.output.write_text(render(audit), encoding='utf-8')
    print(json.dumps({'accepted': audit['accepted'], 'report': str(args.output)}))


if __name__ == '__main__':
    main()
