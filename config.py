import os
import re
from dotenv import load_dotenv

load_dotenv()

# ── Calendar registry ─────────────────────────────────────────────────────────

# Friendly display names keyed by the full calendar URL (no trailing slash).
CALENDAR_NAMES = {
    'https://alice.btc.calendar.opentimestamps.org': 'Alice',
    'https://bob.btc.calendar.opentimestamps.org':   'Bob',
    'https://finney.calendar.eternitywall.com':       'Finney',
    'https://btc.calendar.catallaxy.com':             'Catallaxy',
}

_DEFAULT_CALENDARS = list(CALENDAR_NAMES.keys())

# Override via OTS_CALENDARS env var (comma- or space-separated URLs).
# If unset, all four well-known calendars are used.
_raw_cal = os.getenv('OTS_CALENDARS', '')
OTS_CALENDARS = (
    [u.strip().rstrip('/') for u in re.split(r'[,\s]+', _raw_cal) if u.strip()]
    or _DEFAULT_CALENDARS
)


# ── Flask / DB ────────────────────────────────────────────────────────────────

class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-key-please-change')
    SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URL', 'sqlite:///timestats.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    FILES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app', 'static', 'files')
    OTS_CLI = os.getenv('OTS_CLI', 'ots-cli.js')
    OTS_CALENDARS = OTS_CALENDARS
    CALENDAR_NAMES = CALENDAR_NAMES
