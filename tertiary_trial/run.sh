#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$SCRIPT_DIR/../.venv/bin/python}"

exec "$PYTHON_BIN" "$SCRIPT_DIR/tertiary_trial.py" "$@"
