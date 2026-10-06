# BikeFlow AI

Predictive bike-share rebalancing platform for the EHTP semester innovation project.
The submitted roadmap is in [PLAN.md](PLAN.md). This first implementation session
builds the data foundation only: live station archiving, a full monthly trip audit,
station identifier matching, and a diagnostic station-time table.

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

## Collect station observations

```sh
# Short validation run: three polls respecting the feed TTL.
uv run bikeflow collect --count 3 --interval 60
uv run bikeflow coverage

# Persistent host only; stop with Ctrl-C.
uv run bikeflow collect --count 0 --interval 60
```

Discovery selects station information/status URLs rather than hardcoding their
current host. Each successful poll stores the original discovery, metadata, and
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

`reports/first-session/trip_audit.json` and `station_mapping.csv` contain evidence.
`data/processed/station_time_diagnostic.csv` contains observed 30-minute arrival
and departure bins for twenty busiest verified stations. It is a diagnostic,
not a complete training dataset: absent bins are not automatically zero-filled,
historical station operating history is unknown, and ride IDs are not deduplicated.
Forecasting, weather joins, FastAPI, and the dashboard belong to later sessions.

## Hosted collection while the laptop is off

The **Collect station snapshots** GitHub Actions workflow is scheduled approximately
every five minutes, off the top of the hour, and supports manual execution:

```sh
gh workflow run collect.yml --repo abdelmoghit-elasraoui/BikeFlow-AI
gh run list --workflow collect.yml --repo abdelmoghit-elasraoui/BikeFlow-AI
gh run download RUN_ID --repo abdelmoghit-elasraoui/BikeFlow-AI --dir data/hosted/RUN_ID
```

Each run archives full compressed raw snapshots and a health report as an artifact
for **seven days**. Retrieve artifacts before expiry. GitHub Actions is an initial
hosted collection arrangement, not a guaranteed minute-by-minute service or a
semester-long data archive. Scheduled jobs can be delayed or dropped; public
repository schedules can be disabled after 60 days without repository activity.
Storage is subject to the account quota. Monitor Actions failures and artifact
storage. Transition to persistent object storage and a continuously running worker
before relying on this history for long-term model evaluation. Do not enable paid
storage automatically.

The **Audit monthly trips** workflow scans the same complete February archive on
a hosted runner and exports its report, diagnostic table, and metadata snapshot:

```sh
gh workflow run audit.yml --repo abdelmoghit-elasraoui/BikeFlow-AI
```

For observed collection coverage across downloaded runs:

```sh
uv run bikeflow coverage --raw-root data/hosted
```

An observed gap does not identify whether GitHub scheduling or the external feed
caused it. Individual run health reports and failed workflow logs provide evidence.

## Data sources and interpretation

- [Citi Bike trip data and data-use policy](https://citibikenyc.com/system-data)
- [Monthly trip archive](https://s3.amazonaws.com/tripdata/index.html)
- [Citi Bike GBFS discovery](https://gbfs.citibikenyc.com/gbfs/gbfs.json)
- [GitHub scheduled workflow limitations](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

Trip records provide arrivals/departures; availability snapshots provide station
state. Net inventory changes do not identify separate trip flows. Staff servicing
trips are excluded from the public trip data. Evaluate flow forecasts against trip
records and station warnings against archived states. Current metadata is not proof
of a station's historical capacity or activity.
