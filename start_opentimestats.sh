#!/usr/bin/env bash
# Start OpenTimeStats via Gunicorn.
# Compatible with pm2 — use exec so pm2 tracks the gunicorn PID directly.
#
# Direct usage:
#   ./start_opentimestats.sh
#
# pm2 usage:
#   pm2 start start_opentimestats.sh --name opentimestats --interpreter bash
#
# pm2 ecosystem file (ecosystem.config.js):
#   { name: 'opentimestats', script: './start_opentimestats.sh', interpreter: 'bash' }

set -euo pipefail

export PATH="/usr/local/bin:${PATH}"

APP_DIR="$(cd "$(dirname "$0")" && pwd)"

# Tunable defaults — override via environment variables
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-5000}"
WORKERS="${WORKERS:-2}"

cd "${APP_DIR}"
# shellcheck source=/dev/null
source "${APP_DIR}/venv/bin/activate"

exec gunicorn \
    --bind "${HOST}:${PORT}" \
    --workers "${WORKERS}" \
    --access-logfile - \
    --error-logfile - \
    "run:app"
