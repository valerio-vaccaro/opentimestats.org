#!/usr/bin/env python3
"""
Wipe all data from the database and reload it from the .txt / .ots / .ots.bak
files found in the FILES_DIR directory.

For each <unix>.txt file the script:
  1. Creates a TimestampRequest row (created_at derived from the filename epoch).
  2. Parses the .ots file to find PendingAttestation URIs (still-pending calendars).
  3. Uses the .ots.bak file (if present) to recover the full original calendar list;
     calendars absent from the current .ots pending list are treated as confirmed.
     Falls back to Config.OTS_CALENDARS when no .bak is available.
  4. For confirmed calendars, reads the Bitcoin block height from the .ots file,
     fetches block hash and mined-at timestamp from mempool.space, and writes a
     confirmed CalendarAttestation row.
  5. Sets the request status:
       pending  — no calendars confirmed
       partial  — some confirmed, some still pending
       complete — all calendars confirmed

Run with --commit to write to the database; default is dry-run.

Usage:
  python scripts/reload_from_files.py           # dry-run
  python scripts/reload_from_files.py --commit  # apply
"""
import glob as _glob
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from run import create_app, db
from app.models import CalendarAttestation, TimestampRequest

FILES_DIR      = Config.FILES_DIR
DEFAULT_CALS   = Config.OTS_CALENDARS  # fallback when no .bak file

VENV_SITE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'venv', 'lib',
)

# --------------------------------------------------------------------------- #
# OTS parsing helpers
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
        'pending_uris': [str, ...],          # PendingAttestation calendar URIs
        'block_heights': [int, ...],         # BitcoinBlockHeaderAttestation heights
      }
    Returns empty lists on parse failure.
    """
    try:
        _add_venv_to_path()
        from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
        from opentimestamps.core.timestamp import DetachedTimestampFile
        from opentimestamps.core.serialize import BytesDeserializationContext
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
# mempool.space helpers
# --------------------------------------------------------------------------- #

_block_cache: dict[int, dict] = {}


def get_block_info(height: int) -> dict | None:
    if height in _block_cache:
        return _block_cache[height]
    try:
        hash_bytes = urllib.request.urlopen(
            f"https://mempool.space/api/block-height/{height}", timeout=15
        ).read().decode().strip()
        info = json.loads(urllib.request.urlopen(
            f"https://mempool.space/api/block/{hash_bytes}", timeout=15
        ).read())
        result = {
            'hash': hash_bytes,
            'dt': datetime.fromtimestamp(info['timestamp'], tz=timezone.utc).replace(tzinfo=None),
        }
        _block_cache[height] = result
        return result
    except Exception as exc:
        print(f"    [mempool.space] block {height}: {exc}")
        return None


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    commit = '--commit' in sys.argv
    if not commit:
        print("DRY-RUN mode — pass --commit to write changes to the DB\n")

    # Collect .txt files sorted by filename (= chronological order)
    txt_files = sorted(
        f for f in os.listdir(FILES_DIR) if f.endswith('.txt')
    )
    if not txt_files:
        print(f"No .txt files found in {FILES_DIR}. Nothing to do.")
        return

    print(f"Found {len(txt_files)} .txt file(s) in {FILES_DIR}\n")

    app = create_app()
    with app.app_context():

        # ------------------------------------------------------------------ #
        # Wipe existing data
        # ------------------------------------------------------------------ #
        if commit:
            print("Wiping existing database rows …")
            db.session.execute(db.text('DELETE FROM calendar_attestations'))
            db.session.execute(db.text('DELETE FROM timestamp_requests'))
            db.session.commit()
            print("Done.\n")
        else:
            att_count = CalendarAttestation.query.count()
            req_count = TimestampRequest.query.count()
            print(f"(dry-run) Would delete {att_count} attestation(s) and {req_count} request(s).\n")

        # ------------------------------------------------------------------ #
        # Rebuild from files
        # ------------------------------------------------------------------ #
        for filename in txt_files:
            ots_path = os.path.join(FILES_DIR, filename + '.ots')
            bak_path = os.path.join(FILES_DIR, filename + '.ots.bak')

            # Derive creation time from filename epoch
            try:
                file_unix = int(filename.split('.')[0])
            except ValueError:
                print(f"  SKIP {filename} — cannot parse Unix epoch from filename")
                continue
            created_at = datetime.fromtimestamp(file_unix, tz=timezone.utc).replace(tzinfo=None)

            # Parse current .ots
            if not os.path.exists(ots_path):
                print(f"  SKIP {filename} — no .ots file found")
                continue
            ots_data = _parse_ots(ots_path)
            pending_uris  = set(ots_data['pending_uris'])
            block_heights = ots_data['block_heights']

            # Determine the full original calendar list
            if os.path.exists(bak_path):
                bak_data   = _parse_ots(bak_path)
                all_uris   = set(bak_data['pending_uris'])
                source_tag = "bak"
            elif block_heights:
                # Some calendars confirmed: config calendars not in pending are confirmed.
                # Only valid when block attestations exist — otherwise we can't distinguish
                # "confirmed" from "never participated in this stamp".
                all_uris   = {c.rstrip('/') for c in DEFAULT_CALS}
                source_tag = "config"
            else:
                # No Bitcoin attestations yet and no .bak: treat only the URIs
                # actually present in the .ots file as participants (avoids false
                # "confirmed" for calendars that never responded to the stamp).
                all_uris   = set(pending_uris)
                source_tag = "ots-only"

            # Calendars not in the pending list are confirmed
            confirmed_uris = all_uris - pending_uris
            # Any pending calendar not in all_uris (added later) still shows up
            pending_uris   = pending_uris | (all_uris - confirmed_uris)

            # Fetch block info once (all confirmed calendars share the same aggregated block)
            block_height = min(block_heights) if block_heights else None
            binfo = None
            if block_height is not None:
                binfo = get_block_info(block_height)
                time.sleep(0.3)

            # Determine request status
            n_confirmed = len(confirmed_uris)
            n_total     = len(all_uris | pending_uris)
            if n_confirmed == 0:
                status = 'pending'
            elif n_confirmed >= n_total:
                status = 'complete'
            else:
                status = 'partial'

            print(
                f"  {filename}  created={created_at.isoformat()}  "
                f"status={status}  confirmed={n_confirmed}/{n_total}  "
                f"blk={block_height}  [{source_tag}]"
            )

            if commit:
                req = TimestampRequest(
                    filename=filename,
                    created_at=created_at,
                    status=status,
                )
                db.session.add(req)
                db.session.flush()  # get req.id

                for uri in sorted(all_uris | pending_uris):
                    if uri in confirmed_uris and binfo is not None:
                        att = CalendarAttestation(
                            request_id=req.id,
                            calendar_url=uri,
                            status='confirmed',
                            confirmed_at=binfo['dt'],
                            block_height=block_height,
                            block_hash=binfo['hash'],
                        )
                    else:
                        att = CalendarAttestation(
                            request_id=req.id,
                            calendar_url=uri,
                            status='pending',
                        )
                    db.session.add(att)

        if commit:
            db.session.commit()
            print("\nReload complete.")
        else:
            print("\n(dry-run) Re-run with --commit to apply.")


if __name__ == '__main__':
    main()
