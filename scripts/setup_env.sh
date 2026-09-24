#!/usr/bin/env bash
# Install code only. Datasets are prepared explicitly with prepare_data.py.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$REPO_ROOT/.uv-cache}"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$REPO_ROOT/.uv-python}"
if [[ ! -x .venv/bin/python ]]; then
    uv venv --python "${PYTHON_VERSION:-3.9}" .venv
fi
uv pip install --python .venv/bin/python -e '.[alfworld,dev]'
echo 'Installed SkillExpand. Source scripts/env.sh and configure your model endpoint.'
echo 'ALFWorld data: .venv/bin/python scripts/prepare_data.py alfworld'
echo 'SearchQA: supply your task JSON/JSONL with --task-file.'
