#!/usr/bin/env bash
# Private backup. Usage: backup.sh <new-directory-outside-project> [--config-file <private-file>]
set -euo pipefail
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
PYBIN="$PROJECT/.venv/bin/python"
if [ ! -x "$PYBIN" ]; then PYBIN=python3; fi
exec "$PYBIN" "$PROJECT/scripts/ops_data.py" backup "$@"
