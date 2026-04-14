from __future__ import annotations

import os
import re
import statistics
import subprocess
from datetime import datetime, timezone
from urllib.parse import urlparse

from flask import Blueprint, abort, current_app, jsonify, render_template, request, send_from_directory
from sqlalchemy import func

from app.models import CalendarAttestation, TimestampRequest
from run import db

bp = Blueprint('main', __name__)


# ── Pages ─────────────────────────────────────────────────────────────────────

@bp.route('/')
def index():
    return render_template('index.html')


@bp.route('/charts')
def charts():
    return render_template('charts.html')


@bp.route('/table')
def table():
    return render_template('table.html')


# ── API ───────────────────────────────────────────────────────────────────────

@bp.route('/api/overview')
def api_overview():
    total    = db.session.query(func.count(TimestampRequest.id)).scalar() or 0
    complete = db.session.query(func.count(TimestampRequest.id)).filter_by(status='complete').scalar() or 0
    partial  = db.session.query(func.count(TimestampRequest.id)).filter_by(status='partial').scalar() or 0
    pending  = db.session.query(func.count(TimestampRequest.id)).filter_by(status='pending').scalar() or 0

    confirmed_rows = (
        db.session.query(CalendarAttestation, TimestampRequest)
        .join(TimestampRequest, CalendarAttestation.request_id == TimestampRequest.id)
        .filter(CalendarAttestation.status == 'confirmed',
                CalendarAttestation.confirmed_at.isnot(None))
        .all()
    )

    # Smallest delta per request → average of those
    first_by_req: dict[int, float] = {}
    cal_deltas: dict[str, list[float]] = {}
    for att, req in confirmed_rows:
        d = att.delta_seconds
        if d is None:
            continue
        rid = att.request_id
        if rid not in first_by_req or d < first_by_req[rid]:
            first_by_req[rid] = d
        cal_deltas.setdefault(att.calendar_url, []).append(d)

    first_deltas = list(first_by_req.values())
    avg_first = statistics.mean(first_deltas) if first_deltas else None

    most_responsive = None
    if cal_deltas:
        best_url = min(cal_deltas, key=lambda u: statistics.mean(cal_deltas[u]))
        most_responsive = {
            'calendar_url': best_url,
            'calendar_name': urlparse(best_url).hostname or best_url,
            'avg_delta': statistics.mean(cal_deltas[best_url]),
        }

    cal_count = (
        db.session.query(func.count(func.distinct(CalendarAttestation.calendar_url))).scalar() or 0
    )

    return jsonify({
        'total': total,
        'complete': complete,
        'partial': partial,
        'pending': pending,
        'avg_first_delta': avg_first,
        'most_responsive': most_responsive,
        'calendar_count': cal_count,
    })


@bp.route('/api/calendar-stats')
def api_calendar_stats():
    date_from = request.args.get('date_from', '')
    date_to   = request.args.get('date_to', '')

    query = (
        db.session.query(CalendarAttestation)
        .join(TimestampRequest, CalendarAttestation.request_id == TimestampRequest.id)
    )
    if date_from:
        try:
            query = query.filter(TimestampRequest.created_at >= datetime.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            query = query.filter(TimestampRequest.created_at <= datetime.fromisoformat(date_to))
        except ValueError:
            pass

    atts = query.all()

    data: dict[str, dict] = {}
    for att in atts:
        url = att.calendar_url
        if url not in data:
            data[url] = {
                'calendar_url': url,
                'calendar_name': urlparse(url).hostname or url,
                'confirmed_count': 0,
                'pending_count': 0,
                'block_heights': set(),
                'deltas': [],
            }
        if att.status == 'confirmed' and att.confirmed_at is not None:
            d = att.delta_seconds
            if d is not None:
                data[url]['confirmed_count'] += 1
                data[url]['deltas'].append(d)
            if att.block_height is not None:
                data[url]['block_heights'].add(att.block_height)
        else:
            data[url]['pending_count'] += 1

    result = []
    for entry in data.values():
        deltas = entry['deltas']
        result.append({
            'calendar_url': entry['calendar_url'],
            'calendar_name': entry['calendar_name'],
            'confirmed_count': entry['confirmed_count'],
            'pending_count': entry['pending_count'],
            'distinct_block_count': len(entry['block_heights']),
            'avg_delta': statistics.mean(deltas) if deltas else None,
            'median_delta': statistics.median(deltas) if deltas else None,
            'min_delta': min(deltas) if deltas else None,
            'max_delta': max(deltas) if deltas else None,
        })

    result.sort(key=lambda x: x['avg_delta'] or float('inf'))
    return jsonify(result)


@bp.route('/api/timeline')
def api_timeline():
    date_from = request.args.get('date_from', '')
    date_to   = request.args.get('date_to', '')

    query = TimestampRequest.query
    if date_from:
        try:
            query = query.filter(TimestampRequest.created_at >= datetime.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            query = query.filter(TimestampRequest.created_at <= datetime.fromisoformat(date_to))
        except ValueError:
            pass

    reqs = query.order_by(TimestampRequest.created_at.asc()).all()

    result = []
    for req in reqs:
        deltas = [d for a in req.attestations if (d := a.delta_seconds) is not None]
        if not deltas:
            continue
        result.append({
            'id': req.id,
            'filename': req.filename,
            'created_at': req.created_at.replace(tzinfo=timezone.utc).isoformat(),
            'first_delta': min(deltas),
            'status': req.status,
        })
    return jsonify({'total': len(reqs), 'requests': result})


@bp.route('/api/calendar-timeline')
def api_calendar_timeline():
    date_from = request.args.get('date_from', '')
    date_to   = request.args.get('date_to', '')

    query = (
        db.session.query(CalendarAttestation, TimestampRequest)
        .join(TimestampRequest, CalendarAttestation.request_id == TimestampRequest.id)
        .filter(CalendarAttestation.status == 'confirmed',
                CalendarAttestation.confirmed_at.isnot(None))
    )
    if date_from:
        try:
            query = query.filter(TimestampRequest.created_at >= datetime.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            query = query.filter(TimestampRequest.created_at <= datetime.fromisoformat(date_to))
        except ValueError:
            pass

    rows = query.order_by(TimestampRequest.created_at.asc()).all()

    by_cal: dict[str, dict] = {}
    for att, req in rows:
        url = att.calendar_url
        if url not in by_cal:
            by_cal[url] = {
                'calendar_url': url,
                'calendar_name': att.calendar_name,
                'points': [],
            }
        by_cal[url]['points'].append({
            'created_at': req.created_at.replace(tzinfo=timezone.utc).isoformat(),
            'delta_seconds': att.delta_seconds,  # computed from confirmed_at and filename
            'filename': req.filename,
        })

    return jsonify(list(by_cal.values()))


@bp.route('/api/requests')
def api_requests():
    page     = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 100)

    status_filter   = request.args.get('status', '')
    calendar_filter = request.args.get('calendar', '')
    date_from       = request.args.get('date_from', '')
    date_to         = request.args.get('date_to', '')
    sort_by  = request.args.get('sort_by', 'created_at')
    sort_dir = request.args.get('sort_dir', 'desc')

    _sort_cols = {
        'id':         TimestampRequest.id,
        'created_at': TimestampRequest.created_at,
        'status':     TimestampRequest.status,
    }
    sort_col = _sort_cols.get(sort_by, TimestampRequest.created_at)
    order_expr = sort_col.asc() if sort_dir == 'asc' else sort_col.desc()

    query = TimestampRequest.query

    if status_filter:
        query = query.filter(TimestampRequest.status == status_filter)
    if date_from:
        try:
            query = query.filter(TimestampRequest.created_at >= datetime.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            query = query.filter(TimestampRequest.created_at <= datetime.fromisoformat(date_to))
        except ValueError:
            pass
    if calendar_filter:
        query = (
            query.join(CalendarAttestation)
            .filter(CalendarAttestation.calendar_url == calendar_filter)
        )

    total = query.count()
    reqs  = (
        query.order_by(order_expr)
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    return jsonify({
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': max(1, (total + per_page - 1) // per_page),
        'requests': [r.to_dict() for r in reqs],
    })


@bp.route('/download/<path:filename>')
def download_file(filename):
    # Only serve files that belong to a known request (prevents path traversal)
    base = filename.removesuffix('.ots')
    if not TimestampRequest.query.filter_by(filename=base).first():
        abort(404)
    return send_from_directory(current_app.config['FILES_DIR'], filename, as_attachment=True)


@bp.route('/api/create-now', methods=['POST'])
def api_create_now():
    files_dir = current_app.config['FILES_DIR']
    ots_cli   = current_app.config['OTS_CLI']
    calendars = current_app.config['OTS_CALENDARS']

    now      = datetime.now(timezone.utc).replace(tzinfo=None)
    filename = f"{int(now.timestamp())}.txt"
    os.makedirs(files_dir, exist_ok=True)
    filepath = os.path.join(files_dir, filename)

    with open(filepath, 'w') as fh:
        fh.write("OpenTimestamps proof request\n")
        fh.write(f"Created : {now.isoformat()} UTC\n")
        fh.write(f"Filename: {filename}\n")

    # Stamp on every configured calendar with explicit -c flags
    cmd = [ots_cli, 's']
    for cal in calendars:
        cmd += ['-c', cal]
    cmd.append(filepath)

    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout + result.stderr

    if result.returncode != 0 and 'Submitting to remote calendar' not in output:
        os.unlink(filepath)
        return jsonify({'error': f'ots stamp failed: {output.strip()}'}), 500

    req = TimestampRequest(filename=filename, created_at=now, status='pending')
    db.session.add(req)
    db.session.flush()

    for url in calendars:
        db.session.add(CalendarAttestation(
            request_id=req.id,
            calendar_url=url,
            status='pending',
        ))

    db.session.commit()
    return jsonify({'id': req.id, 'filename': filename, 'calendars': len(calendars)}), 201


@bp.route('/api/calendars')
def api_calendars():
    rows = db.session.query(CalendarAttestation.calendar_url).distinct().all()
    return jsonify([
        {'url': row[0], 'name': urlparse(row[0]).hostname or row[0]}
        for row in rows
    ])
