#!/usr/bin/env bash
# Default performs a real isolated restore. Apply requires empty independent target and fingerprint; never clean.
set -euo pipefail
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
PYBIN="$PROJECT/.venv/bin/python"
if [ ! -x "$PYBIN" ]; then PYBIN=python3; fi
exec "$PYBIN" "$PROJECT/scripts/ops_data.py" restore "$@"
