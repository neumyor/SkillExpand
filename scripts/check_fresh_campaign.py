#!/usr/bin/env python3
"""Run a campaign's independent resume/reviewer/plan checks under its frozen code."""
import argparse
import json
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    root = parser.parse_args().root.resolve()
    manifest = json.loads((root / 'manifest.json').read_text())
    return subprocess.call([manifest['python'], '-u', str(root / 'code' / 'run_campaign.py'),
                            'independent-check', '--root', str(root)])


if __name__ == '__main__':
    raise SystemExit(main())
