#!/usr/bin/env bash
#
# Runtime environment for SkillExpand.  Source it, do not execute it:
#
#     source scripts/env.sh
#     .venv/bin/python scripts/smoke_env.py
#
# Three groups of variables, each set for a measured reason -- see
# See docs/RUNNING.md for the current runtime configuration.

# Resolve the repo root even when sourced from another directory.
_EXPE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 1. Toolchain caches.  uv's default (~/.cache/uv) is not writable under the
#    DSH file sandbox, and uv aborts outright when it cannot create its cache.
export UV_CACHE_DIR="${UV_CACHE_DIR:-$_EXPE_ROOT/.uv-cache}"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$_EXPE_ROOT/.uv-python}"

# 2. Model caches.  This experiment must not download model weights locally:
#    the LLM is served remotely over the tunnel.  ExpeL's default embedder
#    (HuggingFaceEmbeddings/all-mpnet-base-v2) is therefore never used --
#    skill_evolution/embedders.py substitutes a zero-download lexical backend,
#    and refuses to select any weight-downloading one.  The variables below only
#    catch the small tokenizer/plotting artifacts that some libraries insist on
#    writing outside the workspace, where the sandbox denies writes.
export TIKTOKEN_CACHE_DIR="${TIKTOKEN_CACHE_DIR:-$_EXPE_ROOT/.cache-tiktoken}"
export MPLCONFIGDIR="${MPLCONFIGDIR:-$_EXPE_ROOT/.mpl-cache}"

# 3. ALFWorld assets default to the repository's ignored data directory.
export ALFWORLD_DATA="${ALFWORLD_DATA:-$_EXPE_ROOT/data/alfworld}"

# 4. Local settings. Copy .env.example to .env and fill it in before sourcing.
if [ -f "$_EXPE_ROOT/.env" ]; then
    source "$_EXPE_ROOT/.env"
fi

# Thinking must be OFF when the served model is a reasoning model. Measured on
# qwen3.6-flash-distill with ExpeL's stop=['\n','\n\n'] calls: thinking on returns
# an EMPTY content string (~7.8 s) instead of the thought, silently emptying every
# thought and reflection; thinking off returns coherent content in ~1.3 s.
# models/llm.py forwards this as `enable_thinking: false` via model_kwargs.
export EXPE_LLM_DISABLE_THINKING="${EXPE_LLM_DISABLE_THINKING:-1}"

# 4b. Executor competence switch. The engine's admissible action set is appended
#     to the observation. Measured: without it the 7B executor solves 22.2% of
#     tasks and 72.2% of all episodes die from repeating an invalid action (which
#     terminates the episode). Applied identically to every arm, so comparisons
#     stay valid. Set to 0 for an ExpeL-pristine comparison run.
export EXPE_SHOW_ADMISSIBLE="${EXPE_SHOW_ADMISSIBLE:-1}"

# 5. Optional remote embedding service.  Unset by default; executor construction then
#    uses the zero-download lexical backend.  Set EXPE_EMBED_BASE_URL and put
#    embedder_type='remote' in the config to switch.
# export EXPE_EMBED_BASE_URL=http://127.0.0.1:PORT/v1
# export EXPE_EMBED_MODEL=all-mpnet-base-v2

mkdir -p "$UV_CACHE_DIR" "$UV_PYTHON_INSTALL_DIR" "$MPLCONFIGDIR" "$TIKTOKEN_CACHE_DIR"

export PYTHONPATH="$_EXPE_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
unset _EXPE_ROOT
