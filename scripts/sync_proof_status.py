#!/usr/bin/env python3
"""
Re-parse existing .ots files and sync CalendarAttestation / TimestampRequest
status in the DB — without wiping data and without contacting calendar servers.

Useful to bring the DB in sync after manual .ots upgrades or after running
`ots-cli u` by hand outside the normal update loop.

How per-calendar block attribution works
-----------------------------------------
When `ots-cli u` upgrades a proof it does NOT remove a calendar's
PendingAttestation node — it adds Bitcoin block attestations as extra ops
BELOW that same node.  So the .ots tree ends up with:

  PendingAttestation(alice)        ← node still present
    + extra ops → BitcoinBlockHeaderAttestation(944326)   ← added on confirm

  PendingAttestation(finney)       ← still truly pending (no Bitcoin below it)

This means we can determine each calendar's status and exact block from the
.ots file alone:

  • Traverse the tree.
  • For each PendingAttestation node, collect all BitcoinBlockHeaderAttestations
    reachable from that node (via its child ops).
  • If any → calendar confirmed at min(heights).
  • If none → calendar still pending.

No .bak file is required, and each calendar gets its own block height.

Run with --commit to apply; default is dry-run.

Usage:
  python scripts/sync_proof_status.py           # dry-run
  python scripts/sync_proof_status.py --commit  # apply
"""
from __future__ import annotations

import glob as _glob
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from run import create_app, db
from app.models import CalendarAttestation, TimestampRequest

FILES_DIR = Config.FILES_DIR

VENV_SITE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'venv', 'lib',
)

# --------------------------------------------------------------------------- #
# OTS tree analysis
# --------------------------------------------------------------------------- #

def _add_venv_to_path():
    site_dirs = _glob.glob(os.path.join(VENV_SITE, 'python*', 'site-packages'))
    for d in site_dirs:
        if d not in sys.path:
            sys.path.insert(0, d)


def _calendar_status_from_ots(path: str) -> Optional[dict]:
    """
    Traverse an .ots file and return a mapping:
      {calendar_uri: block_height_or_None}

    For each PendingAttestation node, check whether any
    BitcoinBlockHeaderAttestation is reachable from that node via child ops.
    If yes → confirmed at min(heights).  If no → still pending.

    Returns None on parse error.
    """
    try:
        _add_venv_to_path()
        from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
        from opentimestamps.core.serialize import BytesDeserializationContext
        from opentimestamps.core.timestamp import DetachedTimestampFile

        with open(path, 'rb') as fh:
            ctx = BytesDeserializationContext(fh.read())
        det = DetachedTimestampFile.deserialize(ctx)

        result: dict[str, Optional[int]] = {}

        # all_attestations() yields (msg_at_that_node, attestation)
        # We need the *node* itself to look at its child ops.
        # Traverse manually so we have access to each Timestamp node.
        def _walk(ts):
            for att in ts.attestations:
                if isinstance(att, PendingAttestation):
                    uri = att.uri.rstrip('/')
                    # Collect Bitcoin attestations reachable from THIS node's children
                    heights = [
                        a.height
                        for _, a in ts.all_attestations()
                        if isinstance(a, BitcoinBlockHeaderAttestation)
                    ]
                    result[uri] = min(heights) if heights else None
            for child in ts.ops.values():
                _walk(child)

        _walk(det.timestamp)
        return result
    except Exception as exc:
        print(f"    [ots parse] {path}: {exc}")
        return None


# --------------------------------------------------------------------------- #
# mempool.space
# --------------------------------------------------------------------------- #

_block_cache: dict[int, Optional[dict]] = {}


def get_block_info(height: int) -> Optional[dict]:
    if height in _block_cache:
        return _block_cache[height]
    result = None
    try:
        block_hash = urllib.request.urlopen(
            f"https://mempool.space/api/block-height/{height}", timeout=15
        ).read().decode().strip()
        info = json.loads(urllib.request.urlopen(
            f"https://mempool.space/api/block/{block_hash}", timeout=15
        ).read())
        result = {
            'hash': block_hash,
            'dt': datetime.fromtimestamp(info['timestamp'], tz=timezone.utc).replace(tzinfo=None),
        }
    except Exception as exc:
        print(f"    [mempool.space] block {height}: {exc}")
    _block_cache[height] = result
    return result


# --------------------------------------------------------------------------- #
# Per-request sync
# --------------------------------------------------------------------------- #

def sync_request(req: TimestampRequest, commit: bool) -> Optional[list]:
    """
    Sync one request.  Returns a list of change-description strings,
    or None if nothing changed.
    """
    ots_path = os.path.join(FILES_DIR, req.filename + '.ots')
    if not os.path.exists(ots_path):
        return [f"#{req.id} {req.filename}  SKIP — no .ots file"]

    cal_map = _calendar_status_from_ots(ots_path)
    if cal_map is None:
        return [f"#{req.id} {req.filename}  SKIP — parse error"]

    att_by_url: dict[str, CalendarAttestation] = {
        a.calendar_url.rstrip('/'): a for a in req.attestations
    }

    lines = []

    # ------------------------------------------------------------------ #
    # Pass 1 — newly confirmed (pending in DB, confirmed in file)
    # ------------------------------------------------------------------ #
    for uri, blk in cal_map.items():
        if blk is None:
            continue  # still pending in the .ots file
        att = att_by_url.get(uri)
        if att and att.status == 'confirmed':
            continue  # already confirmed in DB — handled by Pass 2

        binfo = get_block_info(blk)
        time.sleep(0.15)

        if att is None:
            att = CalendarAttestation(
                request_id=req.id,
                calendar_url=uri,
                status='pending',
            )
            att_by_url[uri] = att
            if commit:
                db.session.add(att)
                db.session.flush()

        if commit:
            att.status       = 'confirmed'
            att.block_height = blk
            att.block_hash   = binfo['hash'] if binfo else None
            att.confirmed_at = binfo['dt']   if binfo else None

        lines.append(
            f"#{req.id} {req.filename}  NEW confirmed: {uri}  blk={blk}"
        )

    # ------------------------------------------------------------------ #
    # Pass 2 — fix stale block height / confirmed_at on confirmed rows
    # ------------------------------------------------------------------ #
    for uri, blk in cal_map.items():
        if blk is None:
            continue
        att = att_by_url.get(uri)
        if att is None or att.status != 'confirmed':
            continue
        if att.block_height == blk and att.block_hash and att.confirmed_at:
            continue  # already correct

        binfo = get_block_info(blk)
        time.sleep(0.15)
        if binfo is None:
            continue

        old_bh = att.block_height
        old_dt = att.confirmed_at.isoformat() if att.confirmed_at else 'None'
        if commit:
            att.block_height = blk
            att.block_hash   = binfo['hash']
            att.confirmed_at = binfo['dt']
        lines.append(
            f"#{req.id} {req.filename}  FIX {uri}"
            f"  blk {old_bh} → {blk}"
            f"  confirmed_at {old_dt} → {binfo['dt'].isoformat()}"
        )

    if not lines:
        return None

    # ------------------------------------------------------------------ #
    # Recompute request status
    # Only calendars present in the .ots file (cal_map keys) count toward
    # the total — calendars that never participated in this proof are ignored.
    # ------------------------------------------------------------------ #
    total_n     = len(cal_map)
    confirmed_n = sum(1 for blk in cal_map.values() if blk is not None)

    if total_n == 0 or confirmed_n == 0:
        new_status = 'pending'
    elif confirmed_n >= total_n:
        new_status = 'complete'
    else:
        new_status = 'partial'

    if commit:
        req.status = new_status
        db.session.commit()

    if new_status != req.status:
        lines.append(f"#{req.id} {req.filename}  status {req.status} → {new_status}")

    return lines


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    commit = '--commit' in sys.argv
    if not commit:
        print("DRY-RUN mode — pass --commit to write changes to the DB\n")

    app = create_app()
    with app.app_context():
        reqs = (
            TimestampRequest.query
            .filter(TimestampRequest.status != 'complete')
            .order_by(TimestampRequest.created_at.asc())
            .all()
        )
        print(f"Found {len(reqs)} non-complete request(s)\n")

        changed = skipped = 0
        for req in reqs:
            result = sync_request(req, commit)
            if result is None:
                skipped += 1
            else:
                changed += 1
                for line in result:
                    print(f"  {line}")

        print(f"\n{'Applied' if commit else 'Would apply'} {changed} update(s), {skipped} unchanged.")
        if not commit:
            print("Re-run with --commit to apply.")


if __name__ == '__main__':
    main()
