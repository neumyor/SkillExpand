"""Classify one interrupted, unscored E9 verifier as retryable infrastructure.

This preserves the original artifact and never creates a reward or a new attempt.
The normal E9 --resume path may then consume the remaining configured attempt.
"""
import argparse
import json
from pathlib import Path
import shutil
import time


def repair(path: Path):
    path = path.resolve()
    data = json.loads(path.read_text())
    if data.get('reward') is not None or data.get('raw_reward') is not None:
        raise ValueError('artifact already has a reward')
    if data.get('exit_code') != -15 or data.get('failure_kind') != 'evidence_invalid':
        raise ValueError('artifact is not the approved interrupted verifier shape')
    if not (path.parent / 'verifier_stall_recovery.json').exists():
        raise ValueError('missing stall diagnostic')
    backup = path.with_name(f'{path.stem}.before-infra-repair-{time.time_ns()}{path.suffix}')
    shutil.copy2(path, backup)
    data.update(infra=True, interrupted=True, failure_kind='infra_retryable',
                issue='verifier stall terminated after task timeout; no trial result')
    path.write_text(json.dumps(data, indent=2) + '\n')
    return backup


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--artifact', type=Path, required=True)
    print(repair(parser.parse_args().artifact))
