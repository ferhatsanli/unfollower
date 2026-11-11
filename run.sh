#!/usr/bin/env bash
# One-step setup + run. Usage: ./run.sh   |   ./run.sh --demo   |   ./run.sh --refresh
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "python3 not found. Install it with: brew install python" >&2
  exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10+ is required (found $("$PY" --version 2>&1))." >&2
  echo "Install a newer one with: brew install python" >&2
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Creating virtual environment in .venv ..."
  "$PY" -m venv .venv
fi

# Reinstall only when requirements.txt changes
if [ ! -f .venv/.installed ] || ! cmp -s requirements.txt .venv/.installed; then
  echo "Installing dependencies ..."
  .venv/bin/python -m pip install --upgrade pip -q
  .venv/bin/python -m pip install -r requirements.txt -q
  cp requirements.txt .venv/.installed
fi

exec .venv/bin/python unfollow.py "$@"
