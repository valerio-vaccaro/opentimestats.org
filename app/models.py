from datetime import datetime
from urllib.parse import urlparse
from run import db


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
        confirmed_deltas = [a.delta_seconds for a in self.attestations if a.delta_seconds is not None]
        return {
            'id': self.id,
            'filename': self.filename,
            'created_at': self.created_at.isoformat(),
            'status': self.status,
            'attestations': att_dicts,
            'first_delta': min(confirmed_deltas) if confirmed_deltas else None,
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
    # seconds between request creation and confirmation detection
    delta_seconds = db.Column(db.Float, nullable=True)

    __table_args__ = (
        db.UniqueConstraint('request_id', 'calendar_url', name='uq_request_calendar'),
    )

    @property
    def calendar_name(self):
        from config import CALENDAR_NAMES
        url = self.calendar_url.rstrip('/')
        if url in CALENDAR_NAMES:
            return CALENDAR_NAMES[url]
        # Fallback: first meaningful subdomain of the hostname
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
            'confirmed_at': self.confirmed_at.isoformat() if self.confirmed_at else None,
            'block_height': self.block_height,
            'delta_seconds': self.delta_seconds,
        }
