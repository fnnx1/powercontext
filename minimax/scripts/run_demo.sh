#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
: "${MINIMAX_API_KEY:?export MINIMAX_API_KEY first}"
export PYTHONPATH=src
.venv/bin/python -m sdd_context.cli reset
.venv/bin/python -m sdd_context.cli init --reset
.venv/bin/python -m sdd_context.cli test --label before || true
.venv/bin/python -m sdd_context.cli generate
.venv/bin/python -m sdd_context.cli state
