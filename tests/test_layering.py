"""Package layers may import downward only, including function-local imports."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'src' / 'skillexpand'

#: Lower numbers are lower layers.  Modules at the same rank may import each other.
RANK = {
    'schema': 0, 'structured_skill': 0,
    'persistence': 1, 'reliability': 1,
    'benchmarks': 2,
    'runtime': 3,
    'l1': 4,
    'evaluation': 5,
    'l2': 6,
    'campaign': 7, 'cli': 7,
    '__main__': 8,
}


def _imports(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            yield node.lineno, node.module
            if node.module == 'skillexpand':
                for alias in node.names:
                    yield node.lineno, f'skillexpand.{alias.name}'
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name


def test_every_module_belongs_to_a_layer():
    layers = {p.relative_to(ROOT).with_suffix('').parts[0] for p in ROOT.rglob('*.py')}
    assert layers - {'__init__'} <= set(RANK)


def test_no_upward_imports():
    upward = []
    for path in sorted(ROOT.rglob('*.py')):
        source = path.relative_to(ROOT).with_suffix('').parts[0]
        if source == '__init__':
            continue
        for line, module in _imports(path):
            parts = module.split('.')
            if parts[0] != 'skillexpand' or len(parts) < 2 or parts[1] not in RANK:
                continue
            if RANK[parts[1]] > RANK[source]:
                upward.append(f'{path.relative_to(ROOT)}:{line} imports {module}')
    assert not upward, '\n'.join(upward)
