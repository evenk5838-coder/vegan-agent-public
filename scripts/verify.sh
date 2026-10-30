#!/usr/bin/env bash
# Local, offline verification only. Never starts preview/deployment/provider jobs.
set -euo pipefail

TASK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASK_MODE="${1:-all}"
case "$TASK_MODE" in
  all|--backend-only|--frontend-only) ;;
  --help)
    echo 'Usage: bash scripts/verify.sh [--backend-only|--frontend-only]'
    echo 'Default: required files, isolated Python tests, deployment self-check, frontend tests/typecheck/lint.'
    echo 'No provider calls, cloud operations, production build or business data access.'
    exit 0 ;;
  *) echo 'Unsupported verification option.' >&2; exit 2 ;;
esac
if [[ $# -gt 1 ]]; then echo 'Only one verification option is supported.' >&2; exit 2; fi
cd "$TASK_ROOT"

for TASK_FILE in backend/app/main.py backend/alembic.ini backend/requirements.lock.txt \
  frontend/package.json frontend/package-lock.json scripts/run_isolated_tests.py \
  scripts/ops_data.py scripts/ops_health.py scripts/check_persistence.py \
  deploy/compose.yaml deploy/Caddyfile deploy/Dockerfile.api deploy/Dockerfile.web \
  deploy/Dockerfile.ops deploy/runtime.env.example deploy/preflight.py \
  deploy/ops_entry.py deploy/README.md; do
  if [[ ! -f "$TASK_FILE" ]]; then echo "Required file is missing: $TASK_FILE" >&2; exit 1; fi
done

if [[ "$TASK_MODE" != --frontend-only ]]; then
  if [[ ! -x .venv/bin/python ]]; then echo 'Install the locked Python dependencies into .venv first.' >&2; exit 1; fi
  .venv/bin/python scripts/run_isolated_tests.py
  .venv/bin/python deploy/preflight.py --self-test
  bash -n scripts/backup.sh scripts/restore.sh scripts/healthcheck.sh scripts/verify.sh
fi

if [[ "$TASK_MODE" != --backend-only ]]; then
  if [[ ! -d frontend/node_modules ]]; then echo 'Install the locked frontend dependencies with npm ci first.' >&2; exit 1; fi
  command -v npm >/dev/null || { echo 'npm is required for frontend verification.' >&2; exit 1; }
  # npm scripts here are unit tests, tsc and ESLint; none invokes Next or dotenv.
  for TASK_CHECK in test typecheck lint; do
    env -i PATH="$PATH" HOME="${HOME:-/tmp}" LC_ALL=C \
      npm --prefix frontend run "$TASK_CHECK"
  done
fi

echo "Selected local verification passed ($TASK_MODE). Real providers, Docker/Linux, browser/device and cloud deployment remain separate acceptance checks."
