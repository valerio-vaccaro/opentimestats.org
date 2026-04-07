#!/usr/bin/env bash
# Run create_timestamp.py — add to crontab to stamp a new file on schedule.
#
# Example crontab (every 10 minutes):
#   */10 * * * * /path/to/scripts/cron_create.sh >> /var/log/ots_create.log 2>&1

set -euo pipefail

export PATH="/usr/local/bin:${PATH}"

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_PREFIX="[$(date '+%Y-%m-%d %H:%M:%S')] cron_create"

echo "${LOG_PREFIX}: starting"
cd "${APP_DIR}"
# shellcheck source=/dev/null
source "${APP_DIR}/venv/bin/activate"
python scripts/create_timestamp.py
deactivate
echo "${LOG_PREFIX}: done"
