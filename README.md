# BikeFlow AI

Predictive bike-share rebalancing platform for the EHTP semester innovation project.
The submitted roadmap is in [PLAN.md](PLAN.md). This first implementation session
builds the data foundation only: live station archiving, a full monthly trip audit,
station identifier matching, and a diagnostic station-time table.

See `reports/first-session/REPORT.md` (local-only, not pushed to GitHub) for measured results:
1,219,444 trips scanned across both February 2026 CSV parts, 2,130 verified active
station matches, and three successful local live polls.

## Setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
uv sync --locked --extra dev
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

Python 3.12 and dependencies are selected by `.python-version` and `uv.lock`.
No API key is needed. `.env.example` documents optional environment overrides;
the CLI reads exported environment variables, not `.env` files automatically.

## Run and check data on your local PC

```sh
# Short validation run: one poll, prints stations/eligible/stale/raw_path.
uv run bikeflow collect --count 1 --interval 60
# Three polls, ~1 minute apart (respects feed TTL):
uv run bikeflow collect --count 3 --interval 60

# Continuous collection (stop with Ctrl-C):
uv run bikeflow collect --count 0 --interval 60
```

Check what was saved:

```sh
# Coverage summary (polls, gaps, stale/invalid counts, errors):
uv run bikeflow coverage

# Newest raw snapshot + one station row:
ls -t data/raw/gbfs/*/*/*/*.json.gz | head -1
python3 -c "import gzip,json,glob; p=sorted(glob.glob('data/raw/gbfs/*/*/*/*.json.gz'))[-1]; b=json.load(gzip.open(p,'rt')); print(p); print(list(b.keys())); print(b['station_status']['payload']['data']['stations'][0])"

# Timestamps: your fetch time vs feed time vs station time:
python3 -c "import gzip,json,glob,datetime; p=sorted(glob.glob('data/raw/gbfs/*/*/*/*.json.gz'))[-1]; b=json.load(gzip.open(p,'rt')); st=b['station_status']; print('retrieved_at:', st['retrieved_at']); print('feed last_updated:', st['payload']['last_updated']); r=st['payload']['data']['stations'][0]; print('station:', r['station_id'], 'last_reported:', r['last_reported'])"

# SQLite index (eligible=1 usable, eligible=0 closed/stale — keep but don't use):
python3 -c "import sqlite3; db=sqlite3.connect('data/observations.sqlite'); print(db.execute('SELECT id,station_count FROM polls ORDER BY retrieved_at DESC LIMIT 3').fetchall()); print(db.execute(\"SELECT station_id,eligible,issues FROM observations WHERE eligible=1 LIMIT 2\").fetchall())"

# Tests + lint:
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

Each successful poll stores the original discovery, metadata, and
status JSON in `data/raw/gbfs/YYYY/MM/DD/*.json.gz`. Each feed has its retrieval
time, original feed timestamp/version/TTL, and original station reporting times.
SQLite indexes retrievals and normalized eligibility records in
`data/observations.sqlite`. Repeated source responses are identified, not treated
as newly reported states. Errors are recorded separately; no missing observation
is fabricated. Counts, duplicate IDs, operational flags, and freshness are checked.
Closed/stale/invalid stations remain in the raw data but are not eligible for decisions.

SQLite is an initial local collection store, not the final PostgreSQL service.
Raw snapshots can be reprocessed later. Raw data, the database, environments,
and credentials are excluded from Git.

## Audit historical trips

```sh
mkdir -p data/raw/trips
curl --fail --location --retry 3 --output data/raw/trips/202602-citibike-tripdata.zip https://s3.amazonaws.com/tripdata/202602-citibike-tripdata.zip
# Substitute a collected raw snapshot path:
uv run bikeflow audit-trips data/raw/trips/202602-citibike-tripdata.zip \
  --snapshot data/raw/gbfs/YYYY/MM/DD/POLL.json.gz \
  --report-dir reports/first-session
```

The audit streams every data CSV in the ZIP in bounded chunks. It reports schemas,
row totals, timestamp range/problems, missing station IDs, coordinate problems,
and an archive SHA-256. Trip times are interpreted in `America/New_York` and
converted to UTC. Ambiguous/nonexistent DST timestamps are explicitly excluded.

Station matching tests GBFS `short_name` and `station_id`, then checks names and
coordinates within 150 metres. Ambiguous identifiers, distant coordinates,
changed names, inactive stations, and unmatched identifiers are reported for
review. Coordinates alone do not silently authorize a match.

## Session 2: multi-month targets and baseline

```sh
# Download three months (Dec 2025-Feb 2026, ~1 GB total):
for m in 202512 202601 202602; do
  curl --fail --location --retry 3 --output data/raw/trips/$m-citibike-tripdata.zip \
    https://s3.amazonaws.com/tripdata/$m-citibike-tripdata.zip
done
# Build 30/60-min Parquet targets + historical-average baseline v0:
uv run bikeflow session2 --months 202512 202601 202602
```

This reuses the verified mapping (only `matched` IDs are kept), assumes each
station operates between its first and last observed trip day, and holds out
the final 14 days for scoring. Outputs (local-only) land in
`data/processed/session2/`: partitioned Parquet `targets_30min/` and
`targets_60min/` (`year=/month=/`), `station_spans.csv`, `manifest.json`,
and `baseline_v0.json` with MAE per horizon and flow.

`trip_audit.json` and `station_mapping.csv` in your local `reports/first-session/`
contain evidence (local-only, not pushed to GitHub).
`data/processed/station_time_diagnostic.csv` contains observed 30-minute arrival
and departure bins for twenty busiest verified stations. It is a diagnostic,
not a complete training dataset: absent bins are not automatically zero-filled,
historical station operating history is unknown, and ride IDs are not deduplicated.
Forecasting, weather joins, FastAPI, and the dashboard belong to later sessions.

## Data sources and interpretation

- [Citi Bike trip data and data-use policy](https://citibikenyc.com/system-data)
- [Monthly trip archive](https://s3.amazonaws.com/tripdata/index.html)
- [Citi Bike GBFS discovery](https://gbfs.citibikenyc.com/gbfs/gbfs.json)

Trip records provide arrivals/departures; availability snapshots provide station
state. Net inventory changes do not identify separate trip flows. Staff servicing
trips are excluded from the public trip data. Evaluate flow forecasts against trip
records and station warnings against archived states. Current metadata is not proof
of a station's historical capacity or activity.
