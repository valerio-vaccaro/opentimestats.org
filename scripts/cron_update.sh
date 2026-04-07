#!/usr/bin/env bash
# Run update_timestamps.py — add to crontab to upgrade pending OTS proofs.
#
# Example crontab (every 10 minutes, offset 5 min from cron_create):
#   5,15,25,35,45,55 * * * * /path/to/scripts/cron_update.sh >> /var/log/ots_update.log 2>&1

set -euo pipefail

export PATH="/usr/local/bin:${PATH}"

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_PREFIX="[$(date '+%Y-%m-%d %H:%M:%S')] cron_update"

echo "${LOG_PREFIX}: starting"
cd "${APP_DIR}"
# shellcheck source=/dev/null
source "${APP_DIR}/venv/bin/activate"
python scripts/update_timestamps.py
deactivate
echo "${LOG_PREFIX}: done"
