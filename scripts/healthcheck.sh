#!/usr/bin/env bash
# HTTP+DB readiness. Add --local-process-check only for a local non-container API.
set -euo pipefail
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
PYBIN="$PROJECT/.venv/bin/python"
if [ ! -x "$PYBIN" ]; then PYBIN=python3; fi
exec "$PYBIN" "$PROJECT/scripts/ops_health.py" "$@"
