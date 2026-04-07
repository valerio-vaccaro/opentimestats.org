#!/usr/bin/env python3
"""
Upgrade pending OTS proofs and update per-calendar attestation status in the DB.

Each .ots file contains one attestation chain per calendar server.  The upgrade
command contacts only the calendars that still have a pending (non-Bitcoin)
attestation in the file; calendars that already returned a block attestation are
not contacted again.

Suggested crontab (every 10 minutes, offset by 5 min from create_timestamp):
  5,15,25,35,45,55 * * * * cd /path/to/app && /path/to/venv/bin/python scripts/update_timestamps.py >> /var/log/ots_update.log 2>&1
"""
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from run import create_app, db
from app.models import CalendarAttestation, TimestampRequest

OTS_CLI   = os.getenv('OTS_CLI', Config.OTS_CLI)
FILES_DIR = Config.FILES_DIR

_BLOCK_RE = re.compile(r'Bitcoin block (\d+)')
_ATT_RE   = re.compile(r'Got \d+ attestation\(s\) from (https?://\S+)')


def fmt_delta(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.1f}m"
    return f"{seconds/3600:.2f}h"


def upgrade_request(req: TimestampRequest, now: datetime) -> Optional[str]:
    """Upgrade one request. Returns a one-line summary string, or None if skipped."""
    ots_path = os.path.join(FILES_DIR, req.filename + '.ots')
    if not os.path.exists(ots_path):
        return f"#{req.id} MISSING .ots file"

    att_by_url: dict[str, CalendarAttestation] = {
        a.calendar_url.rstrip('/'): a for a in req.attestations
    }
    pending_urls = [u for u, a in att_by_url.items() if a.status == 'pending']

    if not pending_urls:
        req.status = 'complete'
        db.session.commit()
        return None  # already done, nothing to report

    result = subprocess.run(
        [OTS_CLI, 'u', ots_path],
        capture_output=True, text=True,
    )
    output = result.stdout + result.stderr

    newly_confirmed = {url.rstrip('/') for url in _ATT_RE.findall(output)}
    block_height    = int(m.group(1)) if (m := _BLOCK_RE.search(output)) else None

    # Only mark a calendar confirmed when it explicitly appears in the output.
    # "Success! Timestamp complete" means ≥1 Bitcoin attestation exists — NOT
    # that every calendar has confirmed — so it must not bulk-confirm pending ones.
    for url in newly_confirmed:
        att = att_by_url.get(url)
        if att is None:
            att = CalendarAttestation(request_id=req.id, calendar_url=url, status='pending')
            db.session.add(att)
            db.session.flush()
            att_by_url[url] = att
        if att.status != 'confirmed':
            att.status        = 'confirmed'
            att.confirmed_at  = now
            att.delta_seconds = (now - req.created_at).total_seconds()
            att.block_height  = block_height

    # Recompute status strictly from individual calendar attestations:
    #   complete → every calendar confirmed
    #   partial  → at least one confirmed, others still pending
    #   pending  → none confirmed yet
    all_atts    = req.attestations
    confirmed_n = sum(1 for a in all_atts if a.status == 'confirmed')
    total_n     = len(all_atts)

    if total_n > 0 and confirmed_n == total_n:
        req.status = 'complete'
    elif confirmed_n > 0:
        req.status = 'partial'
    else:
        req.status = 'pending'

    db.session.commit()

    # Build compact one-line summary
    cal_parts = []
    for url, att in sorted(att_by_url.items()):
        name = att.calendar_name
        if url in newly_confirmed:
            bh = f" blk={block_height}" if block_height else ""
            cal_parts.append(f"✓{name}(+{fmt_delta(att.delta_seconds)}{bh})")
        elif att.status == 'confirmed':
            cal_parts.append(f"={name}")
        else:
            cal_parts.append(f"…{name}")

    status_tag = {"complete": "COMPLETE", "partial": "partial"}.get(req.status, "pending")
    return f"#{req.id} {req.filename}  [{status_tag}]  {' '.join(cal_parts)}"


def main():
    app = create_app()
    with app.app_context():
        pending_reqs = (
            TimestampRequest.query
            .filter(TimestampRequest.status != 'complete')
            .order_by(TimestampRequest.created_at.asc())
            .all()
        )

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] {len(pending_reqs)} pending request(s)")

        for req in pending_reqs:
            line = upgrade_request(req, now)
            if line:
                print(f"  {line}")

        print("done.")


if __name__ == '__main__':
    main()
