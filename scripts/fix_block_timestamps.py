#!/usr/bin/env python3
"""
Backfill block_height, block_hash, and confirmed_at for confirmed
CalendarAttestation rows using real Bitcoin block timestamps.
delta_seconds is no longer stored — it is computed on the fly from
confirmed_at and the Unix epoch embedded in the filename.

Strategy (in order of preference) for each confirmed attestation:
  1. .ots file present  → parse block height from the Bitcoin attestation embedded in it.
  2. .ots.bak file present → same (backup from the most-recent upgrade step).
  3. Both missing       → binary-search mempool.space for the block mined closest
                          to (created_at + OTS_EXPECTED_MINUTES), which is the
                          expected time the calendar aggregation transaction landed.

The found block height is used to fetch the exact mined-at timestamp and to
recompute delta_seconds = (block_time - request.created_at).

Dry-run mode (default) prints what would change without writing to the DB.
Pass --commit to apply changes.

Usage:
  python scripts/fix_block_timestamps.py           # dry-run
  python scripts/fix_block_timestamps.py --commit  # apply
"""
from __future__ import annotations

import glob as _glob
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from run import create_app, db
from app.models import CalendarAttestation, TimestampRequest

FILES_DIR = Config.FILES_DIR

# How long after file creation OTS typically lands a Bitcoin transaction.
# Used only when no .ots file is available.
OTS_EXPECTED_MINUTES = 20

VENV_SITE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'venv', 'lib',
)

# --------------------------------------------------------------------------- #
# .ots parsing
# --------------------------------------------------------------------------- #

def _add_venv_to_path():
    site_dirs = _glob.glob(os.path.join(VENV_SITE, 'python*', 'site-packages'))
    for d in site_dirs:
        if d not in sys.path:
            sys.path.insert(0, d)


def _parse_ots_height(path: str) -> Optional[int]:
    """Return the lowest BitcoinBlockHeaderAttestation height found in a .ots file."""
    try:
        _add_venv_to_path()
        from opentimestamps.core.notary import BitcoinBlockHeaderAttestation
        from opentimestamps.core.timestamp import DetachedTimestampFile
        with open(path, 'rb') as fh:
            detached = DetachedTimestampFile.deserialize(fh)
        heights = [
            att.height
            for _msg, att in detached.timestamp.all_attestations()
            if isinstance(att, BitcoinBlockHeaderAttestation)
        ]
        return min(heights) if heights else None
    except Exception as exc:
        print(f"    [ots parse] {path}: {exc}")
    return None


def get_block_height_from_file(filename: str) -> Optional[int]:
    """Try .ots then .ots.bak to get a block height. Returns None if neither works."""
    for suffix in ('.ots', '.ots.bak'):
        path = os.path.join(FILES_DIR, filename + suffix)
        if os.path.exists(path):
            h = _parse_ots_height(path)
            if h is not None:
                return h
    return None


# --------------------------------------------------------------------------- #
# mempool.space helpers
# --------------------------------------------------------------------------- #

_block_info_cache: dict[int, Optional[dict]] = {}


def _api_get(url: str, timeout: int = 15):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        data = resp.read()
    return data


def get_block_info(height: int) -> Optional[dict]:
    """
    Return {'hash': str, 'dt': datetime} for a block height via mempool.space.
    Result is cached; 'dt' is a naive UTC datetime.
    """
    if height in _block_info_cache:
        return _block_info_cache[height]
    result = None
    try:
        block_hash = _api_get(
            f"https://mempool.space/api/block-height/{height}"
        ).decode().strip()
        info = json.loads(_api_get(f"https://mempool.space/api/block/{block_hash}"))
        result = {
            'hash': block_hash,
            'dt': datetime.fromtimestamp(info['timestamp'], tz=timezone.utc).replace(tzinfo=None),
        }
    except Exception as exc:
        print(f"    [mempool.space] block {height}: {exc}")
    _block_info_cache[height] = result
    return result


def get_block_timestamp(height: int) -> Optional[datetime]:
    """Convenience wrapper returning only the datetime from get_block_info."""
    info = get_block_info(height)
    return info['dt'] if info else None


def get_current_height() -> int:
    return int(_api_get("https://mempool.space/api/blocks/tip/height").decode().strip())


def _block_ts_unix(height: int) -> Optional[int]:
    """Return the block Unix timestamp (int) for the binary search."""
    info = get_block_info(height)
    if info is None:
        return None
    return int(info['dt'].replace(tzinfo=timezone.utc).timestamp())


_APPROX_BLOCK_SECS = 577  # empirical average (Bitcoin mines slightly faster than 600s)


def estimate_height_for_ts(target_unix: int, tip_height: int, tip_unix: int) -> int:
    """Estimate block height from Unix timestamp, anchored to the current tip."""
    delta = target_unix - tip_unix
    est = tip_height + int(delta / _APPROX_BLOCK_SECS)
    return max(0, min(est, tip_height))


def filename_to_unix(filename: str) -> Optional[int]:
    """
    Extract the Unix timestamp embedded in the filename (e.g. '1775550325.txt' → 1775550325).
    Returns None if the filename does not contain a numeric stem.
    """
    try:
        return int(filename.split('.')[0])
    except (ValueError, IndexError):
        return None


def find_block_height_by_timestamp(
    created_unix: int,
    tip_height: int,
    tip_unix: int,
) -> Optional[int]:
    """
    Binary-search for the block whose mined timestamp is closest to
    (created_unix + OTS_EXPECTED_MINUTES).  The found block must have been
    mined after created_unix (otherwise no attestation can exist yet).
    Returns the block height or None on failure.
    """
    target_unix = created_unix + OTS_EXPECTED_MINUTES * 60

    # Wide search window: lo = 50 blocks before creation, hi = 6h after target + 50 blocks.
    # This handles both slow-confirming calendars and block-time variance.
    lo = max(0, estimate_height_for_ts(created_unix, tip_height, tip_unix) - 50)
    hi = min(estimate_height_for_ts(target_unix + 6 * 3600, tip_height, tip_unix) + 50, tip_height)

    target_dt_str = datetime.fromtimestamp(target_unix, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    print(f"    [binary search] target={target_dt_str} UTC  range=[{lo}, {hi}]")

    # Find the highest block with timestamp <= target_unix.
    iterations = 0
    while lo < hi:
        if iterations > 30:
            print("    [binary search] too many iterations, stopping")
            break
        mid = (lo + hi + 1) // 2
        mid_ts = _block_ts_unix(mid)
        if mid_ts is None:
            print(f"    [binary search] failed to fetch block {mid}")
            return None
        if mid_ts <= target_unix:
            lo = mid
        else:
            hi = mid - 1
        iterations += 1
        time.sleep(0.2)

    # lo is now the highest block with ts <= target_unix.
    # We want the first block AFTER created_unix (i.e. the earliest valid confirmation).
    # Walk forward from lo until we find such a block.
    candidate = lo
    for _ in range(10):
        ts = _block_ts_unix(candidate)
        if ts is None:
            break
        if ts > created_unix:
            return candidate
        candidate += 1
        time.sleep(0.2)

    print(f"    [binary search] could not find a block after created_unix={created_unix} near height {lo}")
    return None


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    commit = '--commit' in sys.argv
    if not commit:
        print("DRY-RUN mode — pass --commit to write changes to the DB\n")

    app = create_app()
    with app.app_context():
        attestations = (
            CalendarAttestation.query
            .filter(CalendarAttestation.status == 'confirmed')
            .order_by(CalendarAttestation.request_id.asc(), CalendarAttestation.id.asc())
            .all()
        )
        print(f"Found {len(attestations)} confirmed attestation(s) to process.\n")

        # Fetch current tip once to seed binary search estimates
        try:
            tip_height = get_current_height()
            tip_time   = get_block_timestamp(tip_height)
            if tip_time is None:
                raise RuntimeError("could not fetch tip block timestamp")
            tip_unix = int(tip_time.replace(tzinfo=timezone.utc).timestamp())
            print(f"Current Bitcoin tip: block {tip_height} ({tip_time.isoformat()} UTC)\n")
        except Exception as exc:
            print(f"Could not fetch current block height: {exc}")
            sys.exit(1)

        # Cache per-request: filename → block_height (avoid re-parsing / re-searching)
        _req_height_cache: dict[int, Optional[int]] = {}

        updated = skipped = 0

        for att in attestations:
            req: TimestampRequest = att.request

            # ---------------------------------------------------------------- #
            # Step 1: determine block height
            # ---------------------------------------------------------------- #
            if att.block_height is not None:
                height = att.block_height
                source = "db"
            elif req.id in _req_height_cache:
                height = _req_height_cache[req.id]
                source = "cache"
            else:
                # Try .ots / .ots.bak first
                height = get_block_height_from_file(req.filename)
                if height is not None:
                    source = "ots_file"
                else:
                    # Binary search anchored on the Unix timestamp in the filename
                    # (avoids DB timezone ambiguity; filename always encodes true UTC epoch).
                    file_unix = filename_to_unix(req.filename)
                    if file_unix is None:
                        print(f"  req#{req.id} {req.filename}: SKIP — cannot parse Unix timestamp from filename")
                        _req_height_cache[req.id] = None
                        skipped += 1
                        continue
                    target_dt_iso = datetime.fromtimestamp(
                        file_unix + OTS_EXPECTED_MINUTES * 60, tz=timezone.utc
                    ).strftime('%Y-%m-%dT%H:%M:%S')
                    print(f"  req#{req.id} {req.filename}: no .ots file — binary searching for block ~{target_dt_iso} UTC")
                    height = find_block_height_by_timestamp(file_unix, tip_height, tip_unix)
                    source = "estimated"
                _req_height_cache[req.id] = height

            if height is None:
                print(f"  att#{att.id} ({att.calendar_name}): SKIP — could not determine block height")
                skipped += 1
                continue

            # ---------------------------------------------------------------- #
            # Step 2: fetch block info (hash + timestamp)
            # ---------------------------------------------------------------- #
            binfo = get_block_info(height)
            if binfo is None:
                print(f"  att#{att.id} ({att.calendar_name}): SKIP — API returned no info for block {height}")
                skipped += 1
                continue
            block_time = binfo['dt']
            block_hash = binfo['hash']

            # ---------------------------------------------------------------- #
            # Step 3: apply new values (delta_seconds is computed, not stored)
            # ---------------------------------------------------------------- #
            old_confirmed = att.confirmed_at.isoformat() if att.confirmed_at else "None"
            old_bh = att.block_height

            changed = (
                att.block_height != height
                or att.block_hash  != block_hash
                or att.confirmed_at != block_time
            )

            est_tag = " [estimated]" if source == "estimated" else ""
            if not changed:
                print(f"  att#{att.id} ({att.calendar_name}): already correct  blk={height} {block_time.isoformat()}{est_tag}")
                continue

            # Show computed delta for informational purposes
            file_unix = filename_to_unix(req.filename)
            if file_unix is not None:
                import calendar as _cal
                new_delta = _cal.timegm(block_time.timetuple()) - file_unix
                delta_str = f"{new_delta}s"
            else:
                delta_str = "n/a"

            print(
                f"  att#{att.id} req#{req.id} ({att.calendar_name}): "
                f"blk {old_bh} → {height} ({block_hash[:12]}…){est_tag}  "
                f"confirmed_at {old_confirmed} → {block_time.isoformat()}  "
                f"delta={delta_str}  [{source}]"
            )

            if commit:
                att.block_height = height
                att.block_hash   = block_hash
                att.confirmed_at = block_time
                updated += 1

            time.sleep(0.1)

        if commit:
            db.session.commit()
            print(f"\nCommitted {updated} update(s), skipped {skipped}.")
        else:
            print(f"\n(dry-run) Skipped {skipped}. Re-run with --commit to apply.")


if __name__ == '__main__':
    main()
