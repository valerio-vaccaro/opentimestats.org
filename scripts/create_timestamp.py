#!/usr/bin/env python3
"""
Create a new file, submit it to all configured OTS calendars, and record the
request in the DB with one CalendarAttestation row per calendar.

Suggested crontab (every 10 minutes):
  */10 * * * * cd /path/to/app && /path/to/venv/bin/python scripts/create_timestamp.py >> /var/log/ots_create.log 2>&1
"""
import os
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from run import create_app, db
from app.models import CalendarAttestation, TimestampRequest

OTS_CLI   = os.getenv('OTS_CLI', Config.OTS_CLI)
FILES_DIR = Config.FILES_DIR
CALENDARS = Config.OTS_CALENDARS


def main():
    now      = datetime.now(timezone.utc).replace(tzinfo=None)
    filename = f"{int(now.timestamp())}.txt"

    os.makedirs(FILES_DIR, exist_ok=True)
    filepath = os.path.join(FILES_DIR, filename)

    with open(filepath, 'w') as fh:
        fh.write("OpenTimestamps proof request\n")
        fh.write(f"Created : {now.isoformat()} UTC\n")
        fh.write(f"Filename: {filename}\n")

    print(f"[{now.isoformat()}] Created {filepath}")
    print(f"Stamping on {len(CALENDARS)} calendar(s): {', '.join(CALENDARS)}")

    # Build command: ots-cli.js s -c <url1> -c <url2> ... <file>
    cmd = [OTS_CLI, 's']
    for cal in CALENDARS:
        cmd += ['-c', cal]
    cmd.append(filepath)

    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout + result.stderr
    print(output.strip())

    if result.returncode != 0 and 'Submitting to remote calendar' not in output:
        print(f"ERROR: ots stamp failed (rc={result.returncode})")
        os.unlink(filepath)
        sys.exit(1)

    app = create_app()
    with app.app_context():
        req = TimestampRequest(filename=filename, created_at=now, status='pending')
        db.session.add(req)
        db.session.flush()

        # Pre-create one attestation row for every configured calendar so the
        # DB is consistent from the start — no need to parse CLI output.
        for url in CALENDARS:
            db.session.add(CalendarAttestation(
                request_id=req.id,
                calendar_url=url,
                status='pending',
            ))

        db.session.commit()
        print(f"Saved request id={req.id} with {len(CALENDARS)} calendar(s)")


if __name__ == '__main__':
    main()
