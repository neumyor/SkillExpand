"""Freeze audited inputs/settings/source bytes before any real generation or trial."""
import sys
from pathlib import Path
from state import ROOT,read,write,freeze_bytes

def main():
    if (ROOT/'bindings.json').exists():raise ValueError('Bindings already frozen')
    if not read(ROOT/'input_preaudit.json').get('passed'):raise ValueError('Input audit not passed')
    if not read(ROOT/'runner_preaudit.json').get('passed'):raise ValueError('Runner audit not passed')
    manifest=read(ROOT/'manifest.json')
    manifest.update(preaudit_passed=True,status='frozen_ready')
    write(ROOT/'manifest.json',manifest)
    files=[p for p in ROOT.glob('*.py')]
    files += [ROOT/'protocol.md',ROOT/'manifest.json',ROOT/'input_audit.md',ROOT/'runner_preaudit.md',ROOT/'input_preaudit.json',ROOT/'runner_preaudit.json']
    files += [p for p in (ROOT/'code').rglob('*') if p.is_file() and p.suffix=='.py']
    files += [p for p in (ROOT/'frozen').rglob('*') if p.is_file() and '__pycache__' not in p.parts]
    for task in manifest['tasks']:
        # Task files are evaluator-only. Read bytes solely to bind the unchanged environment/verifier.
        files += [p for p in (ROOT/'tasks'/task/'frozen').rglob('*') if p.is_file() and '__pycache__' not in p.parts and 'runtime' not in p.relative_to(ROOT/'tasks'/task/'frozen').parts]
    rows=[]
    for i,p in enumerate(sorted(set(files))):
        snapshot=ROOT/'binding_snapshots'/f'{i:04d}.bin'
        freeze_bytes(snapshot,p.read_bytes())
        rows.append({'current':str(p.relative_to(ROOT)),'snapshot':str(snapshot.relative_to(ROOT))})
    write(ROOT/'bindings.json',rows)
    print('Frozen files:',len(rows))
if __name__=='__main__':main()
