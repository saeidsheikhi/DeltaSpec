#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip wheel setuptools
pip install -e ".[dev]"

echo
echo "Base EffectGate environment installed."
echo "Next:"
echo "  cp configs/.env.example .env   # if not already done"
echo "  source .venv/bin/activate"
echo "  python scripts/check_ollama.py"
echo "  pytest -q"
echo "  python scripts/smoke_test.py"
echo
echo "AppWorld is intentionally not installed by default."
echo "After the 48h core loop works, follow scripts/install_appworld.sh"
