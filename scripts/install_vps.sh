#!/usr/bin/env bash
set -euo pipefail
PYTHON_BIN="${PYTHON_BIN:-python3.11}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN=python3
fi
"$PYTHON_BIN" -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip wheel setuptools
pip install -r requirements.txt
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env. Edit it with: nano .env"
fi
echo "Install complete. Run: source .venv/bin/activate && python main.py"
