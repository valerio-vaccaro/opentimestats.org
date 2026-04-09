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
import json
import os
import re
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from run import create_app, db
from app.models import CalendarAttestation, TimestampRequest

OTS_CLI   = os.getenv('OTS_CLI', Config.OTS_CLI)
FILES_DIR = Config.FILES_DIR

_ATT_RE = re.compile(r'Got \d+ attestation\(s\) from (https?://\S+)')

VENV_SITE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'venv', 'lib',
)


def _get_block_height_from_ots(ots_path: str) -> Optional[int]:
    """Return the lowest Bitcoin block height embedded in a completed .ots file, or None."""
    try:
        import glob as _glob
        site_dirs = _glob.glob(os.path.join(VENV_SITE, 'python*', 'site-packages'))
        for d in site_dirs:
            if d not in sys.path:
                sys.path.insert(0, d)
        from opentimestamps.core.notary import BitcoinBlockHeaderAttestation
        from opentimestamps.core.serialize import BytesDeserializationContext
        from opentimestamps.core.timestamp import DetachedTimestampFile
        with open(ots_path, 'rb') as fh:
            ctx = BytesDeserializationContext(fh.read())
        detached = DetachedTimestampFile.deserialize(ctx)
        heights = [
            att.height
            for _msg, att in detached.timestamp.all_attestations()
            if isinstance(att, BitcoinBlockHeaderAttestation)
        ]
        return min(heights) if heights else None
    except Exception as exc:
        print(f"    [ots parse] {exc}")
    return None


def _get_block_info(height: int) -> Optional[dict]:
    """
    Fetch block hash and mined-at UTC datetime for a given block height.
    Returns {'hash': str, 'dt': datetime} or None on error.
    """
    try:
        url_hash = f"https://mempool.space/api/block-height/{height}"
        with urllib.request.urlopen(url_hash, timeout=10) as resp:
            block_hash = resp.read().decode().strip()
        url_info = f"https://mempool.space/api/block/{block_hash}"
        with urllib.request.urlopen(url_info, timeout=10) as resp:
            info = json.loads(resp.read())
        return {
            'hash': block_hash,
            'dt': datetime.fromtimestamp(info['timestamp'], tz=timezone.utc).replace(tzinfo=None),
        }
    except Exception as exc:
        print(f"    [mempool.space] {exc}")
    return None


def _filename_to_unix(filename: str) -> Optional[int]:
    """Extract the Unix epoch from a filename like '1775550325.txt'."""
    try:
        return int(filename.split('.')[0])
    except (ValueError, IndexError):
        return None


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

    # Derive block height, hash and mined-at timestamp from the .ots file so we
    # use the real block time instead of the wall-clock detection time.
    block_height: Optional[int]   = None
    block_hash:   Optional[str]   = None
    block_time:   Optional[datetime] = None
    if newly_confirmed:
        block_height = _get_block_height_from_ots(ots_path)
        if block_height is not None:
            binfo = _get_block_info(block_height)
            if binfo is not None:
                block_hash = binfo['hash']
                block_time = binfo['dt']

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
            confirmed_time   = block_time if block_time is not None else now
            att.status       = 'confirmed'
            att.confirmed_at = confirmed_time
            att.block_height = block_height
            att.block_hash   = block_hash

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
            src = " (block time)" if block_time is not None else " (wall clock)"
            cal_parts.append(f"✓{name}(+{fmt_delta(att.delta_seconds)}{bh}{src})")
        elif att.status == 'confirmed':
            cal_parts.append(f"={name}")
        else:
            cal_parts.append(f"…{name}")

    status_tag = {"complete": "COMPLETE", "partial": "partial"}.get(req.status, "pending")
    return f"#{req.id} {req.filename}  [{status_tag}]  {' '.join(cal_parts)}"


ABANDON_AFTER_DAYS = 7
MAX_WORKERS = 10


def _upgrade_in_thread(app, req_id: int, now: datetime) -> Optional[str]:
    """Run upgrade_request for a single request inside its own app context."""
    with app.app_context():
        req = db.session.get(TimestampRequest, req_id)
        if req is None:
            return None
        return upgrade_request(req, now)


def main():
    app = create_app()
    with app.app_context():
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        cutoff = now - timedelta(days=ABANDON_AFTER_DAYS)

        pending_reqs = (
            TimestampRequest.query
            .filter(TimestampRequest.status != 'complete')
            .order_by(TimestampRequest.created_at.asc())
            .all()
        )

        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] {len(pending_reqs)} pending request(s)")

        # Split into abandoned (too old) and active
        abandoned_ids = []
        active_ids    = []
        for req in pending_reqs:
            if req.created_at < cutoff:
                req.status = 'complete'
                abandoned_ids.append(req.id)
            else:
                active_ids.append(req.id)

        if abandoned_ids:
            db.session.commit()
            print(f"  Abandoned {len(abandoned_ids)} request(s) older than {ABANDON_AFTER_DAYS} days.")

    # Process active requests in parallel (each worker opens its own app context)
    if active_ids:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(_upgrade_in_thread, app, req_id, now): req_id
                for req_id in active_ids
            }
            for future in as_completed(futures):
                try:
                    line = future.result()
                except Exception as exc:
                    req_id = futures[future]
                    print(f"  #{req_id} ERROR: {exc}")
                else:
                    if line:
                        print(f"  {line}")

    print("done.")


if __name__ == '__main__':
    main()
