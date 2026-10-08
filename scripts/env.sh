#!/usr/bin/env bash
#
# Runtime environment for SkillExpand.  Source it, do not execute it:
#
#     source scripts/env.sh
#     .venv/bin/python scripts/smoke_env.py
#
# See docs/RUNNING.md for the full runtime configuration.

# Resolve the repo root even when sourced from another directory.
_EXPE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 1. Toolchain caches.  uv's default (~/.cache/uv) is not writable under the
#    DSH file sandbox, and uv aborts outright when it cannot create its cache.
export UV_CACHE_DIR="${UV_CACHE_DIR:-$_EXPE_ROOT/.uv-cache}"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$_EXPE_ROOT/.uv-python}"

# 2. Tokenizer/plotting caches that some libraries write outside the workspace,
#    where the sandbox denies writes.  No model weights are downloaded locally:
#    the LLM is served remotely.
export TIKTOKEN_CACHE_DIR="${TIKTOKEN_CACHE_DIR:-$_EXPE_ROOT/.cache-tiktoken}"
export MPLCONFIGDIR="${MPLCONFIGDIR:-$_EXPE_ROOT/.mpl-cache}"

# 3. ALFWorld assets default to the repository's ignored data directory.
export ALFWORLD_DATA="${ALFWORLD_DATA:-$_EXPE_ROOT/data/alfworld}"

# 4. Local settings. Copy .env.example to .env and fill it in before sourcing.
#    Campaigns (python -m skillexpand.campaign / scripts/run_campaign.py) also
#    require ALFWORLD_PYTHON, ALFWORLD_CONFIG, ALFWORLD_BENCH_SRC and
#    EXPE_CAMPAIGN_OVERLAY from .env.
if [ -f "$_EXPE_ROOT/.env" ]; then
    source "$_EXPE_ROOT/.env"
fi

# 5. Thinking must be OFF when the served model is a reasoning model. Measured on
#    qwen3.6-flash-distill with stop=['\n','\n\n'] calls: thinking on returns an
#    EMPTY content string (~7.8 s) instead of the thought; thinking off returns
#    coherent content in ~1.3 s.  runtime/models/llm.py forwards this as
#    `enable_thinking: false`.
export EXPE_LLM_DISABLE_THINKING="${EXPE_LLM_DISABLE_THINKING:-1}"

# 6. Executor competence switch. The engine's admissible action set is appended
#    to the observation. Measured: without it the 7B executor solves 22.2% of
#    tasks and 72.2% of all episodes die from repeating an invalid action (which
#    terminates the episode). Applied identically to every arm, so comparisons
#    stay valid. Set to 0 for an ExpeL-pristine comparison run.
export EXPE_SHOW_ADMISSIBLE="${EXPE_SHOW_ADMISSIBLE:-1}"

mkdir -p "$UV_CACHE_DIR" "$UV_PYTHON_INSTALL_DIR" "$MPLCONFIGDIR" "$TIKTOKEN_CACHE_DIR"

export PYTHONPATH="$_EXPE_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
unset _EXPE_ROOT
