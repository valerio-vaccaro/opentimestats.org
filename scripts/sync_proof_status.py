#!/usr/bin/env python3
"""
Re-parse existing .ots files and sync CalendarAttestation / TimestampRequest
status in the DB — without wiping data and without contacting calendar servers.

Useful to bring the DB in sync after manual .ots upgrades or after running
`ots-cli u` by hand outside the normal update loop.

What this script does
----------------------
For every non-complete TimestampRequest it performs two passes:

Pass 1 — detect newly-confirmed calendars
  When `ots-cli u` upgrades a proof, each calendar that has mined a Bitcoin
  transaction has its PendingAttestation replaced by a BitcoinBlockHeaderAttestation
  in the .ots file.  The .bak file is the snapshot *before* the last upgrade.

  Participant list priority:
    1. .ots.bak  — pending URIs before last upgrade = authoritative list
    2. DB rows   — calendar_url values already recorded for this request

  Confirmed = participants no longer pending in the current .ots.
  For each newly-confirmed calendar the block mined-at timestamp and hash
  are fetched from mempool.space.

Pass 2 — fix block height / date for already-confirmed attestations
  Confirmed attestations may have a wall-clock confirmed_at (set at detection
  time) or a missing / wrong block_height.  This pass re-reads the .ots file,
  extracts the canonical block height, fetches the real mined-at timestamp, and
  overwrites stale values.

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

FILES_DIR      = Config.FILES_DIR
CONFIG_CALS    = {c.rstrip('/') for c in Config.OTS_CALENDARS}

VENV_SITE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'venv', 'lib',
)

# --------------------------------------------------------------------------- #
# OTS parsing
# --------------------------------------------------------------------------- #

def _add_venv_to_path():
    site_dirs = _glob.glob(os.path.join(VENV_SITE, 'python*', 'site-packages'))
    for d in site_dirs:
        if d not in sys.path:
            sys.path.insert(0, d)


def _parse_ots(path: str) -> dict:
    """
    Parse an .ots file and return:
      {
        'pending_uris':  [str, ...],   # PendingAttestation calendar URIs
        'block_heights': [int, ...],   # BitcoinBlockHeaderAttestation heights
      }
    Returns empty lists on parse failure.
    """
    try:
        _add_venv_to_path()
        from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
        from opentimestamps.core.serialize import BytesDeserializationContext
        from opentimestamps.core.timestamp import DetachedTimestampFile
        with open(path, 'rb') as fh:
            ctx = BytesDeserializationContext(fh.read())
        detached = DetachedTimestampFile.deserialize(ctx)
        pending_uris  = []
        block_heights = []
        for _msg, att in detached.timestamp.all_attestations():
            if isinstance(att, PendingAttestation):
                pending_uris.append(att.uri.rstrip('/'))
            elif isinstance(att, BitcoinBlockHeaderAttestation):
                block_heights.append(att.height)
        return {'pending_uris': pending_uris, 'block_heights': block_heights}
    except Exception as exc:
        print(f"    [ots parse] {path}: {exc}")
        return {'pending_uris': [], 'block_heights': []}


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

def sync_request(req: TimestampRequest, commit: bool) -> Optional[list[str]]:
    """
    Sync one request by comparing .ots / .ots.bak against DB attestations.
    Returns a list of change lines, or None if nothing changed.
    """
    ots_path = os.path.join(FILES_DIR, req.filename + '.ots')
    bak_path = os.path.join(FILES_DIR, req.filename + '.ots.bak')

    if not os.path.exists(ots_path):
        return [f"#{req.id} {req.filename}  SKIP — no .ots file"]

    # Current state of the proof
    ots = _parse_ots(ots_path)
    pending_now  = {u for u in ots['pending_uris']}
    block_height = min(ots['block_heights']) if ots['block_heights'] else None

    # Build the broadest possible participant set:
    #   .bak  — URIs before last upgrade (may already be post-confirmation)
    #   db    — calendar_url values recorded for this request
    #   When block attestations are present, also include Config.OTS_CALENDARS so
    #   that calendars whose PendingAttestation was removed in a prior upgrade cycle
    #   (and is therefore absent from both .bak and DB) are still accounted for.
    db_urls = {a.calendar_url.rstrip('/') for a in req.attestations}

    if os.path.exists(bak_path):
        bak = _parse_ots(bak_path)
        bak_uris = {u for u in bak['pending_uris']}
        source_tag = 'bak+db'
    else:
        bak_uris = set()
        source_tag = 'db'

    if block_height is not None:
        # Confirmations have occurred: widen the net to include config calendars
        # so we can detect URIs lost from .bak and DB across multiple upgrade cycles.
        all_participants = bak_uris | db_urls | CONFIG_CALS
        source_tag += '+config'
    else:
        all_participants = bak_uris | db_urls

    confirmed_by_file = all_participants - pending_now

    att_by_url: dict[str, CalendarAttestation] = {
        a.calendar_url.rstrip('/'): a for a in req.attestations
    }

    # Fetch block info once (lazy, shared across both passes)
    binfo: Optional[dict] = None

    def _ensure_binfo():
        nonlocal binfo
        if binfo is None and block_height is not None:
            binfo = get_block_info(block_height)
            time.sleep(0.2)

    lines: list[str] = []

    # ------------------------------------------------------------------ #
    # Pass 1: detect pending → confirmed transitions
    #   • update existing DB rows still marked pending
    #   • create DB rows for confirmed calendars with no record at all
    #     (happens when a calendar confirmed before the DB row was created)
    # ------------------------------------------------------------------ #
    newly_confirmed = [
        url for url in confirmed_by_file
        if url not in att_by_url or att_by_url[url].status != 'confirmed'
    ]

    if newly_confirmed:
        _ensure_binfo()
        for url in newly_confirmed:
            if url in att_by_url:
                att = att_by_url[url]
            else:
                att = CalendarAttestation(
                    request_id=req.id,
                    calendar_url=url,
                    status='pending',
                )
                att_by_url[url] = att
                if commit:
                    db.session.add(att)
                    db.session.flush()

            if commit:
                att.status       = 'confirmed'
                att.confirmed_at = binfo['dt']   if binfo else None
                att.block_height = block_height
                att.block_hash   = binfo['hash'] if binfo else None

        bh_str  = f"blk={block_height}" if block_height else "blk=?"
        cal_str = ', '.join(sorted(newly_confirmed))
        lines.append(
            f"#{req.id} {req.filename}  NEW confirmed: [{cal_str}]"
            f"  {bh_str}  [{source_tag}]"
        )

    # ------------------------------------------------------------------ #
    # Pass 2: fix block height / confirmed_at on already-confirmed rows
    # ------------------------------------------------------------------ #
    if block_height is not None:
        for url, att in att_by_url.items():
            if att.status != 'confirmed':
                continue
            needs_fix = (
                att.block_height != block_height
                or att.block_hash is None
                or att.confirmed_at is None
            )
            if not needs_fix:
                continue
            _ensure_binfo()
            if binfo is None:
                continue
            old_bh  = att.block_height
            old_dt  = att.confirmed_at.isoformat() if att.confirmed_at else 'None'
            if commit:
                att.block_height = block_height
                att.block_hash   = binfo['hash']
                att.confirmed_at = binfo['dt']
            lines.append(
                f"#{req.id} {req.filename}  FIX {url}"
                f"  blk {old_bh} → {block_height}"
                f"  confirmed_at {old_dt} → {binfo['dt'].isoformat()}"
            )

    if not lines:
        return None

    # Recompute request status
    will_be_confirmed = {
        url for url, att in att_by_url.items()
        if att.status == 'confirmed' or url in confirmed_by_file
    }
    conf_n  = len(will_be_confirmed)
    total_n = len(att_by_url)

    if conf_n == 0:
        new_status = 'pending'
    elif conf_n >= total_n:
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
