import calendar as _cal
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse
from run import db


def _utc_iso(dt: datetime) -> str:
    """Return an ISO-8601 string with explicit UTC offset for a naive-UTC datetime."""
    return dt.replace(tzinfo=timezone.utc).isoformat()


class TimestampRequest(db.Model):
    __tablename__ = 'timestamp_requests'

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # pending → partial (some calendars confirmed) → complete (all confirmed)
    status = db.Column(db.String(20), nullable=False, default='pending')

    attestations = db.relationship(
        'CalendarAttestation',
        backref='request',
        lazy=True,
        cascade='all, delete-orphan',
    )

    def to_dict(self):
        att_dicts = [a.to_dict() for a in self.attestations]
        confirmed_atts = [a for a in self.attestations if a.delta_seconds is not None]
        confirmed_deltas = [a.delta_seconds for a in confirmed_atts]
        first_att = min(confirmed_atts, key=lambda a: a.delta_seconds, default=None)
        return {
            'id': self.id,
            'filename': self.filename,
            'created_at': _utc_iso(self.created_at),
            'status': self.status,
            'attestations': att_dicts,
            'first_delta': min(confirmed_deltas) if confirmed_deltas else None,
            'first_confirmed_at': _utc_iso(first_att.confirmed_at) if first_att and first_att.confirmed_at else None,
            'first_block_height': first_att.block_height if first_att else None,
            'full_delta': max(confirmed_deltas) if self.status == 'complete' and confirmed_deltas else None,
        }


class CalendarAttestation(db.Model):
    __tablename__ = 'calendar_attestations'

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey('timestamp_requests.id'), nullable=False)
    calendar_url = db.Column(db.String(500), nullable=False)
    # pending → confirmed
    status = db.Column(db.String(20), nullable=False, default='pending')
    confirmed_at = db.Column(db.DateTime, nullable=True)
    block_height = db.Column(db.Integer, nullable=True)
    block_hash   = db.Column(db.String(64), nullable=True)

    __table_args__ = (
        db.UniqueConstraint('request_id', 'calendar_url', name='uq_request_calendar'),
    )

    @property
    def delta_seconds(self) -> Optional[float]:
        """
        Seconds from file creation (filename epoch) to Bitcoin block confirmation.
        Computed on the fly — not stored in the database.
        Uses calendar.timegm so the naive confirmed_at datetime is always
        interpreted as UTC, regardless of server timezone.
        """
        if self.confirmed_at is None:
            return None
        try:
            file_unix = int(self.request.filename.split('.')[0])
        except (ValueError, AttributeError):
            return None
        confirmed_unix = _cal.timegm(self.confirmed_at.timetuple())
        return float(confirmed_unix - file_unix)

    @property
    def calendar_name(self):
        from config import CALENDAR_NAMES
        url = self.calendar_url.rstrip('/')
        if url in CALENDAR_NAMES:
            return CALENDAR_NAMES[url]
        try:
            host = urlparse(url).hostname or url
            first = host.split('.')[0]
            return first if first not in ('', 'www') else host
        except Exception:
            return self.calendar_url

    def to_dict(self):
        return {
            'id': self.id,
            'calendar_url': self.calendar_url,
            'calendar_name': self.calendar_name,
            'status': self.status,
            'confirmed_at': _utc_iso(self.confirmed_at) if self.confirmed_at else None,
            'block_height': self.block_height,
            'block_hash': self.block_hash,
            'delta_seconds': self.delta_seconds,
        }
