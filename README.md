# OpenTimeStats

A Flask web application that submits files to [OpenTimestamps](https://opentimestamps.org) calendar servers, records how long each calendar takes to produce a Bitcoin blockchain attestation, and visualises the results over time.

---

## Table of contents

1. [What it does](#what-it-does)
2. [How OpenTimestamps works](#how-opentimestamps-works)
3. [Prerequisites](#prerequisites)
4. [Installation](#installation)
5. [Configuration](#configuration)
6. [Database setup](#database-setup)
7. [Running the application](#running-the-application)
8. [Cron jobs](#cron-jobs)
9. [Web interface](#web-interface)
10. [API reference](#api-reference)
11. [Downloading files and proofs](#downloading-files-and-proofs)
12. [Data model](#data-model)
13. [Project layout](#project-layout)

---

## What it does

Every 10 minutes a cron job creates a small text file, submits it to the standard OTS calendar servers via `ots-cli.js`, and records the pending request in a database. A second cron job tries to upgrade every pending proof; when a calendar server returns a Bitcoin block attestation the confirmation time is saved. The web UI provides three views and a JSON API.

All timestamps — file creation and block confirmation — are stored and displayed in **GMT (UTC)**.

---

## How OpenTimestamps works

OpenTimestamps is an open protocol for trustlessly proving that a piece of data existed at a certain point in time, using the Bitcoin blockchain as a notary.

1. **Stamping** — `ots-cli.js s <file>` hashes the file, submits the hash to a set of calendar servers, and writes a `.ots` proof file. Each calendar aggregates many hashes per block into a Merkle tree and periodically commits the root to a Bitcoin transaction.

2. **Upgrading** — `ots-cli.js u <file>.ots` asks each calendar for its Bitcoin attestation. Once a calendar has included the hash in a mined block it returns the attestation and the `.ots` file is updated in place. A `.ots.bak` backup is kept on each upgrade.

3. **Verifying** — `ots-cli.js v <file>.ots` confirms the proof against the Bitcoin blockchain without trusting the calendar servers.

OpenTimeStats automates steps 1 and 2, parses the resulting `.ots` file to extract the confirmed Bitcoin block height, fetches the block's mined-at timestamp from the mempool.space public API, and records that as the authoritative confirmation time.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | Tested on 3.11 and 3.13 |
| Node.js 16+ | Required to run `ots-cli.js` |
| `ots-cli.js` | OpenTimestamps JavaScript CLI — must be on `PATH` |
| MySQL 5.7+ **or** SQLite | SQLite works out of the box for local development |

### Install the OpenTimestamps JS client

```bash
npm install -g javascript-opentimestamps
ots-cli.js --help    # verify it is available
```

---

## Installation

```bash
git clone <repo-url>
cd opentimestats.org

python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

`requirements.txt` installs Flask, Flask-SQLAlchemy, Flask-Migrate, python-dotenv, and PyMySQL (a pure-Python MySQL driver — no C library required).

---

## Configuration

Edit `.env` in the project root (create it if it does not exist):

```bash
cp .env.example .env    # or create from scratch
```

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | `dev-key-please-change` | Flask session secret. **Change this in production.** |
| `DATABASE_URL` | `sqlite:///timestats.db` | SQLAlchemy connection string (see below) |
| `OTS_CLI` | `ots-cli.js` | Command or full path to the OTS CLI binary |
| `FLASK_RUN_HOST` | `127.0.0.1` | Bind address |
| `FLASK_RUN_PORT` | `5050` | Listen port |

### SQLite (local / development)

No extra setup. Leave `DATABASE_URL` unset, or set:

```
DATABASE_URL=sqlite:///timestats.db
```

### MySQL (production)

```
DATABASE_URL=mysql+pymysql://opentimestats:password@localhost/opentimestats
```

Create the database and user first:

```sql
CREATE DATABASE opentimestats CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'opentimestats'@'localhost' IDENTIFIED BY 'password';
GRANT ALL PRIVILEGES ON opentimestats.* TO 'opentimestats'@'localhost';
```

---

## Database setup

### First time

```bash
flask db upgrade        # applies all migrations in order
```

If you prefer to skip the migration system entirely and just create the tables directly:

```bash
flask init-db
```

### After schema changes

```bash
flask db migrate -m "short description"
flask db upgrade
```

---

## Running the application

```bash
flask run
# or
python run.py
```

The app is available at `http://127.0.0.1:5050` by default. Change `FLASK_RUN_HOST` and `FLASK_RUN_PORT` in `.env` to expose it on a different address or port.

For production, run behind a WSGI server such as Gunicorn:

```bash
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:5050 "run:app"
```

---

## Cron jobs

Two scripts handle the automated workflow. Add both to your crontab (`crontab -e`), replacing the paths:

```cron
# Create a new timestamped file every 10 minutes
*/10 * * * *          cd /path/to/app && /path/to/venv/bin/python scripts/create_timestamp.py >> /var/log/ots_create.log 2>&1

# Upgrade pending proofs every 10 minutes, offset by 5 minutes
5,15,25,35,45,55 * * * *   cd /path/to/app && /path/to/venv/bin/python scripts/update_timestamps.py >> /var/log/ots_update.log 2>&1
```

The 5-minute offset ensures proofs are first checked about 5 minutes after creation, then every 10 minutes thereafter. Bitcoin block times average ~10 minutes, so most confirmations are detected within 20 minutes of stamping.

### `scripts/create_timestamp.py`

Runs every 10 minutes. Steps:

1. Generates a filename from the current Unix timestamp: `{unix_ts}.txt`.
2. Writes the file to `app/static/files/` with a short human-readable header.
3. Calls `ots-cli.js s <filepath>`, which contacts the calendar servers and writes `<filepath>.ots`.
4. Inserts a `TimestampRequest` row (`status = pending`) and one `CalendarAttestation` row per calendar (`status = pending`) into the database.
5. If `ots-cli.js` fails and no calendars were contacted, the `.txt` file is deleted and the script exits with an error.

### `scripts/update_timestamps.py`

Runs every 10 minutes (offset). Steps:

1. Queries all `TimestampRequest` rows whose status is not `complete`.
2. For each request, calls `ots-cli.js u <filepath>.ots` to attempt upgrading the proof.
3. Parses `"Got N attestation(s) from <url>"` lines in the output. For each newly confirmed calendar:
   - Reads the upgraded `.ots` file using the Python `opentimestamps` library to extract the confirmed Bitcoin block height.
   - Fetches the block's mined-at timestamp from the [mempool.space](https://mempool.space) public API.
   - Sets `CalendarAttestation.status = confirmed`, `confirmed_at` (block mined time in GMT), `block_height`, `block_hash`, and `delta_seconds` (seconds from file creation to block confirmation).
4. Updates the parent `TimestampRequest.status`:
   - `partial` — at least one calendar confirmed, but not all
   - `complete` — all calendars confirmed
5. Requests already marked `complete` are skipped entirely.

### `scripts/fix_block_timestamps.py`

One-shot backfill script for existing records that are missing `block_height`, `block_hash`, or have an inaccurate `confirmed_at`. Run it manually after schema upgrades or to correct historical data.

```bash
source venv/bin/activate

# Dry-run (prints what would change, no writes)
python scripts/fix_block_timestamps.py

# Apply changes
python scripts/fix_block_timestamps.py --commit
```

For each confirmed attestation the script:

1. If a `.ots` or `.ots.bak` file exists — parses the block height directly from the Bitcoin attestation embedded in the proof.
2. Otherwise — binary-searches the Bitcoin blockchain (via mempool.space) for the block mined closest to the file's creation time.
3. Fetches the block hash and mined-at timestamp from mempool.space.
4. Updates `block_height`, `block_hash`, `confirmed_at` (GMT), and `delta_seconds`.

---

## Web interface

### Dashboard (`/`)

- **Stats row** — total files, complete, partial, pending (live, auto-refreshes every 60 s)
- **Info row** — average time to first confirmation, most-responsive calendar with its average delta, number of active calendars
- **Recent requests table** — the 10 most recent requests with status, first-confirmation time, and download buttons
- **New timestamp button** — manually triggers a stamp outside the cron schedule (see below)

#### New timestamp button

Calls `POST /api/create-now`. The button shows a spinner while `ots-cli.js` contacts the calendar servers (typically 2–5 seconds), then turns green on success or red on failure. The stats and recent table refresh automatically after a successful stamp.

### Charts (`/charts`)

Both charts share a **date-range filter** with four preset buttons:

| Preset | Range |
|---|---|
| Last day | Past 24 hours |
| Last week | Past 7 days *(default on page load)* |
| Last month | Past 30 days |
| Last year | Past 365 days |

The date inputs can also be edited manually; doing so clears the active preset highlight.

**Confirmation time per calendar** (bar chart)

Shows four grouped bars per calendar — Min, Median, Average, Max — so you can compare both the typical and worst-case performance of each server. The Y-axis is in minutes.

**Time to first confirmation per file** (scatter chart)

Each dot represents one timestamped file. The X-axis is the file creation date; the Y-axis is the number of minutes until the first calendar confirmed. Dots are coloured by status: green = complete, yellow = partial, grey = pending.

### Table (`/table`)

A server-side paginated (25 rows per page) table of all requests. All dates are displayed in **GMT**.

Columns:

| Column | Description |
|---|---|
| # | Request ID (sortable) |
| File | Filename (`{unix_ts}.txt`) |
| Created (GMT) | Timestamp when the file was created (sortable) |
| Status | `pending` / `partial` / `complete` badge (sortable) |
| First confirm | Delta to first attestation, confirmation date (GMT), and block height |
| Download | File and proof download buttons |
| *[calendar name]* | One column per known calendar — delta, confirmation date (GMT), and block height linked to blockstream.info |

**Filters** — combinable:

| Filter | Description |
|---|---|
| Status | `pending`, `partial`, or `complete` |
| Calendar | Restrict to requests that involved a specific calendar |
| From / To | Date range (also controlled by the preset buttons) |

Clicking the `#`, `Created`, or `Status` column headers sorts the table ascending/descending. The active sort column is highlighted with an arrow indicator.

---

## API reference

All endpoints return JSON. No authentication is required. All datetime fields are ISO 8601 strings in UTC (e.g. `2026-04-07T08:25:25+00:00`).

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/overview` | High-level counters and averages for the dashboard |
| `GET` | `/api/calendar-stats` | Per-calendar min / median / avg / max confirmation time |
| `GET` | `/api/timeline` | First-confirmation delta per file (scatter chart data) |
| `GET` | `/api/requests` | Paginated, filterable, sortable request list with attestation detail |
| `GET` | `/api/calendars` | Distinct calendar URLs and short names seen so far |
| `POST` | `/api/create-now` | Create and stamp a new file immediately |
| `GET` | `/download/<filename>` | Download a `.txt` file or `.ots` proof (see below) |

### `GET /api/overview` — response fields

| Field | Type | Description |
|---|---|---|
| `total` | int | Total number of requests |
| `complete` | int | Requests with all calendars confirmed |
| `partial` | int | Requests with some calendars confirmed |
| `pending` | int | Requests with no confirmations yet |
| `avg_first_delta` | float\|null | Average seconds to first confirmation across all requests |
| `most_responsive` | object\|null | `{calendar_url, calendar_name, avg_delta}` for the fastest calendar |
| `calendar_count` | int | Number of distinct calendars seen |

### `GET /api/calendar-stats` — response fields (array)

| Field | Type | Description |
|---|---|---|
| `calendar_url` | string | Full calendar URL |
| `calendar_name` | string | Hostname extracted from the URL |
| `confirmed_count` | int | Number of confirmed attestations from this calendar |
| `pending_count` | int | Number of still-pending attestations |
| `avg_delta` | float\|null | Average confirmation time in seconds |
| `median_delta` | float\|null | Median confirmation time in seconds |
| `min_delta` | float\|null | Fastest confirmation in seconds |
| `max_delta` | float\|null | Slowest confirmation in seconds |

### `GET /api/requests` — query parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `page` | int | `1` | Page number |
| `per_page` | int | `20` | Rows per page (max `100`) |
| `status` | string | — | Filter: `pending`, `partial`, or `complete` |
| `calendar` | string | — | Filter by exact calendar URL |
| `date_from` | ISO 8601 | — | Earliest `created_at` to include |
| `date_to` | ISO 8601 | — | Latest `created_at` to include |
| `sort_by` | string | `created_at` | Sort column: `id`, `created_at`, or `status` |
| `sort_dir` | string | `desc` | Sort direction: `asc` or `desc` |

### `GET /api/requests` — attestation fields (per calendar)

| Field | Type | Description |
|---|---|---|
| `calendar_url` | string | Full calendar URL |
| `calendar_name` | string | Short display name |
| `status` | string | `pending` or `confirmed` |
| `confirmed_at` | ISO 8601\|null | Bitcoin block mined-at time (GMT) |
| `block_height` | int\|null | Bitcoin block number |
| `block_hash` | string\|null | Bitcoin block hash (hex, 64 chars) |
| `delta_seconds` | float\|null | Seconds from file creation to block confirmation |

### `GET /api/timeline` — query parameters

Accepts the same `date_from` / `date_to` parameters as `/api/requests`.

### `POST /api/create-now` — response

```json
{ "id": 42, "filename": "1746279007.txt", "calendars": 4 }
```

Returns HTTP 201 on success, 500 with `{"error": "..."}` if `ots-cli.js` fails.

---

## Downloading files and proofs

The route `GET /download/<filename>` serves files from `app/static/files/` as browser downloads. It accepts:

- `GET /download/1746279007.txt` — the original text file
- `GET /download/1746279007.txt.ots` — the OTS proof

**Security** — before serving any file, the route strips the `.ots` suffix (if present) and checks that the base filename exists as a `TimestampRequest` in the database. Requests for unknown filenames or path-traversal attempts (e.g. `../../config.py`) return HTTP 404.

In the UI, each request row shows two icon buttons:

| Icon | Button | Enabled when |
|---|---|---|
| `bi-file-text` | Download file | Always |
| `bi-shield-check` (blue) | Download OTS proof | Always — the `.ots` file contains pending calendar attestations even before Bitcoin confirms |

---

## Data model

```
TimestampRequest
  id            INTEGER   PK
  filename      VARCHAR   unique — e.g. "1746279007.txt"
                          the stem is the Unix epoch of creation (authoritative, timezone-free)
  created_at    DATETIME  stored in server local time; use the filename stem for UTC calculations
  status        VARCHAR   pending | partial | complete

CalendarAttestation
  id            INTEGER   PK
  request_id    INTEGER   FK → TimestampRequest.id
  calendar_url  VARCHAR   e.g. "https://alice.btc.calendar.opentimestamps.org"
  status        VARCHAR   pending | confirmed
  confirmed_at  DATETIME  GMT — mined-at timestamp of the confirming Bitcoin block
  block_height  INTEGER   Bitcoin block number (nullable until confirmed)
  block_hash    VARCHAR   Bitcoin block hash, 64 hex chars (nullable until confirmed)
  delta_seconds FLOAT     seconds from file creation (filename epoch) to block confirmation
```

`TimestampRequest.status` state machine:

```
pending  ──(first calendar confirms)──►  partial  ──(all calendars confirm)──►  complete
```

**`delta_seconds` accuracy** — measures the interval between the Unix epoch embedded in the filename and the Bitcoin block's mined-at timestamp (fetched from mempool.space). This reflects the actual time the blockchain confirmed the attestation, independent of when the monitoring script happened to run.

---

## Project layout

```
opentimestats.org/
├── run.py                          # Flask app factory, db/migrate init, CLI commands
├── config.py                       # All configuration (reads .env)
├── requirements.txt
├── .env                            # Local environment variables — do not commit
├── migrations/
│   └── versions/
│       ├── 0001_initial_schema.py  # Creates timestamp_requests and calendar_attestations
│       └── 0002_add_block_hash.py  # Adds block_hash column to calendar_attestations
├── app/
│   ├── __init__.py                 # Empty package marker
│   ├── models.py                   # TimestampRequest, CalendarAttestation
│   ├── routes.py                   # All Flask routes and JSON API endpoints
│   ├── templates/
│   │   ├── base.html               # Bootstrap 5 navbar, Chart.js CDN, shared JS utils
│   │   ├── index.html              # Dashboard
│   │   ├── charts.html             # Charts with date-range presets
│   │   └── table.html              # Paginated, filterable, sortable table
│   └── static/
│       ├── css/style.css           # Minimal overrides on top of Bootstrap
│       ├── js/charts.js            # Chart.js data loading and rendering
│       └── files/                  # Runtime: .txt files and .ots proofs stored here
└── scripts/
    ├── create_timestamp.py         # Cron: create file, call ots stamp, insert DB row
    ├── update_timestamps.py        # Cron: upgrade proofs, record block confirmations
    └── fix_block_timestamps.py     # One-shot: backfill block_height/hash/confirmed_at
```
