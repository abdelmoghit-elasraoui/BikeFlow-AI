# BikeFlow AI — project guide

This document explains the project from scratch: the problem, the moving
parts, what has been built so far, and what Sprint 1 specifically delivered.
It assumes no prior context beyond basic Python and web knowledge.

---

## 1. The problem

Bike-share systems like Citi Bike have a physical imbalance problem:

- Commuters ride **into** Midtown in the morning, so residential stations
  empty and office stations fill up.
- The reverse happens in the evening.
- If a station has **no bikes**, riders cannot start a trip.
- If a station has **no free docks**, riders cannot end a trip.

Operators pay crews and trucks to move bikes around ("rebalancing"), and they
need to decide **where to send a truck next** and **how many bikes to move**.
Today this is largely reactive: drivers visit stations that are already empty
or full.

BikeFlow AI is a decision-support prototype: forecast bike departures and
arrivals for the next 30 and 60 minutes per station, combine those forecasts
with live availability, and rank stations by how urgently they need attention.

It does **not** control trucks, does not integrate with Citi Bike operations,
and makes no causal claim that following the queue saves money. Its honest
output is measured forecast accuracy and a transparent priority list.

---

## 2. System architecture at a glance

Four environments are involved:

```
                    Citi Bike public data
                 ┌────────────┴────────────┐
                 │                         │
      GBFS live feed              Monthly trip archives
   (station status, 60s)           (CSV inside ZIP, S3)
                 │                         │
                 ▼                         ▼
    ┌─────────────────────┐      ┌─────────────────────┐
    │  VPS (Windows 10)   │      │  Mac (development)  │
    │                     │      │                     │
    │  1. Collector       │      │  1. Trip audit      │
    │     every 60s       │      │  2. Parquet targets │
    │  2. SQLite index    │      │  3. Model training  │
    │  3. FastAPI         │      │  4. Tests / lint    │
    │  4. Prediction log  │      │                     │
    └──────────┬──────────┘      └──────────┬──────────┘
               │                            │
               │ SSH tunnel (localhost:8000)│
               ▼                            ▼
        Mac browser / curl           artifacts → VPS (scp)
               │
               ▼
       GitHub (code only, no data)
```

### Roles

| Machine | Role | Why |
| --- | --- | --- |
| **VPS** | Runs 24/7 collection and serves the API | The laptop sleeps; live history must not depend on it |
| **Mac** | Development: data processing, training, tests | Heavy CPU work happens offline; artifacts are copied to the VPS |
| **GitHub** | Source of truth for code | Data, databases, models, and secrets stay out of Git |
| **Citi Bike** | External data source | GBFS live feeds + monthly trip archives |

### What runs where

**VPS — two Windows scheduled tasks:**

1. `BikeFlowCollector`
   ```
   uv run bikeflow collect --count 0 --interval 60
   ```
   Every ~60 seconds: download GBFS discovery, station information, and
   station status; write a compressed raw snapshot; insert rows into SQLite;
   log one JSON line to `C:\BikeFlow\data\collect.log`.

2. `BikeFlowAPI`
   ```
   bikeflow.exe --data-dir C:\BikeFlow\data serve --version v1 \
     --models C:\BikeFlow\data\models --host 127.0.0.1 --port 8000
   ```
   Loads the four LightGBM models, reads the newest snapshot, serves JSON
   endpoints, and writes every forecast to the `predictions` table.

**Mac — interactive work:**

- Downloads trip archives and builds Parquet targets (`bikeflow session2`).
- Trains and evaluates models (`bikeflow train`).
- Runs the test suite and lint.
- Opens an SSH tunnel to reach the VPS API as `localhost:8000`.

**GitHub — code and documentation only.** `data/`, `.venv/`, `*.sqlite` are
ignored. Models are transferred to the VPS with `scp`, not committed.

---

## 3. Data sources

### 3.1 Form of the live GBFS data

Everything arrives as **plain JSON over HTTPS**. There is no XML, no
authentication, no API key, and no database. The data has three levels.

**Level 1 — discovery directory** (`gbfs.json`): a list of feed names and URLs.

```json
{"data": {"en": {"feeds": [
  {"name": "station_information", "url": "https://gbfs.lyft.com/gbfs/1.1/bkn/en/station_information.json"},
  {"name": "station_status",      "url": "https://gbfs.lyft.com/gbfs/1.1/bkn/en/station_status.json"}
]}}}
```

The collector reads this first and follows the URLs; hosts and versions are
never hardcoded.

**Level 2 — station information** (metadata, one object per station):

```json
{
  "station_id": "66db408f-0aca-11e7-82f6-3863bb44ef7c",
  "name": "Harrison St & Hudson St",
  "short_name": "5400.05",
  "lat": 40.71870987, "lon": -74.0090009,
  "capacity": 39,
  "region_id": "71",
  "station_type": "classic",
  "rental_methods": ["KEY", "CREDITCARD"]
}
```

**Level 3 — station status** (dynamic state, changes every ~60 seconds):

```json
{"last_updated": 1791967946, "ttl": 60, "version": "1.1",
 "data": {"stations": [{
   "station_id": "66db408f-0aca-11e7-82f6-3863bb44ef7c",
   "num_bikes_available": 33,
   "num_bikes_disabled": 3,
   "num_docks_available": 3,
   "num_docks_disabled": 0,
   "is_installed": 1, "is_renting": 1, "is_returning": 1,
   "last_reported": 1791967736,
   "legacy_id": "249",
   "num_ebikes_available": 8
 }]}}
```

Format details that matter:

- **Times are Unix epoch seconds** (integers), not ISO strings. They are
  converted to ISO UTC when stored.
- **Flags are 0/1 integers**, not booleans.
- **`ttl: 60`** declares the feed valid for 60 seconds; the collector never
  polls faster.
- **`last_reported` is per station** and can lag `last_updated`. That lag is how
  stale stations are detected.
- A status payload holds about 2,519 stations and compresses to roughly 205 KB.

### 3.2 Are the three feeds updated at the same time?

No. They are three independent HTTP requests with three independent
timestamps. In one stored bundle:

| Feed | Retrieved at | Feed `last_updated` | Meaning |
| --- | --- | --- | --- |
| discovery | 08:53:33.207 | 1791968005 | Feed directory, changes rarely |
| station_information | 08:53:34.181 | 1791967946 | Station metadata, changes rarely |
| station_status | 08:53:34.978 | 1791967946 | Live state, every ~60s |

Consequences:

- The three fetches are **not atomic**. A station could be added or removed
  between the information and status calls. The collector tolerates this: a
  status row without metadata is flagged `missing_metadata` and excluded from
  decisions, not dropped.
- Each stored envelope keeps **its own retrieval time** (`retrieved_at`) and
  **its own feed time** (`last_updated`), so the difference is auditable later.
- Discovery and station information do not need 60-second polling in principle,
  but fetching them together is cheap and guarantees the metadata matches the
  status snapshot.

### 3.3 How staleness is detected

Three separate checks, all with explicit thresholds:

1. **Feed-level**: if `now − last_updated > max(300 s, 3 × ttl)`, every station in
   that poll is flagged `stale_feed`. A 60-second TTL therefore allows 3
   minutes before the whole feed is considered stale.
2. **Station-level**: if `now − last_reported > max(300 s, 3 × ttl)`, that station
   is flagged `stale_station` and marked `eligible = 0`. It stays archived for
   analysis but cannot drive a forecast or a recommendation.
3. **Future timestamps**: a feed or station timestamp more than 60 seconds in
   the future is flagged (`future_feed_timestamp`, `future_station_timestamp`),
   which catches clock errors on either side.

The API repeats the freshness check rather than trusting the collector: every
forecast response carries `snapshot_stale`, and `/freshness` reports
`age_seconds`. A snapshot older than 10 minutes is never presented as current.
The two historical SSL failures and the 47-hour VPS gap remain visible in the
`errors` table and `coverage` output — the system records outages instead of
hiding them.

### 3.4 The historical data: where it comes from and what is in it

Source: Citi Bike publishes one ZIP per month at
`https://s3.amazonaws.com/tripdata/YYYYMM-citibike-tripdata.zip`. Each ZIP holds
one or more CSVs. The project currently uses **December 2025, January 2026, and
February 2026**, totalling **5,127,455 trips** (about 1 GB compressed).

Each row contains:

| Field | Meaning |
| --- | --- |
| `ride_id` | Unique trip identifier |
| `rideable_type` | classic bike, electric bike, scooter |
| `started_at`, `ended_at` | Local New York time, no timezone |
| `start_station_name`, `start_station_id` | Where the trip began |
| `end_station_name`, `end_station_id` | Where it ended |
| `start_lat`, `start_lng`, `end_lat`, `end_lng` | Coordinates |
| `member_casual` | Subscription type |

What is **missing or problematic**, and how the project handles it:

| Gap | Consequence | Current handling |
| --- | --- | --- |
| **No rebalancing records** | Inventory change ≠ rider demand; staff move bikes invisibly | Evaluate flows against trips (unaffected), evaluate warning quality separately against snapshots; state that causal benefit is unproven |
| **No station opening/closing dates** | Cannot know when a station truly operated | First/last observed trip day heuristic; windows outside are absent, never zero; documented in `docs/eligibility.md` |
| **No capacity history** | Current capacity is not February capacity | Use current capacity with a staleness caveat; sensitivity checks planned; archived snapshots will build real capacity history over time |
| **Missing station IDs** (1,119 start, 8,058 end in Feb) | Some trips cannot be attributed | Excluded from targets; counted and reported; never guessed from coordinates alone |
| **Unmatched IDs** (71 stations) | Historical stations absent from live feed | Excluded from training; kept in the mapping report for review |
| **Month boundary spillover** (trips dated outside the nominal month) | Double counting risk across months | Windows assigned by the trip's own timestamp; overlap dedup verified (0 duplicates in 5.1M) |
| **Censored demand** | When a station is empty, unmet demand is invisible; trips that never happen are not recorded | Acknowledge as a limitation; evaluate forecasting accuracy, not true demand; classify warnings against observed empty/full events |
| **No weather** | Rain/snow suppress demand | Planned Open-Meteo join using archived forecast runs available at prediction time |
| **No holidays** | Holidays behave like weekends | `day_of_year` partially captures it; explicit calendar flags planned |
| **Staff trips excluded** | Public data omits operator rides | No impact on rider demand targets; noted in the report |
| **Timezones not provided** | Local times are ambiguous at DST transitions | Interpreted as `America/New_York`; ambiguous/nonexistent timestamps are excluded, not guessed |

The general principle: **missing data is excluded and reported, never invented.**
That is why the collector records failures, the targets leave gaps explicit, and
the reports list every excluded category with counts.

### 3.5 How each poll is stored

1. **Raw** — the complete discovery + information + status JSON, gzipped, at
   `data/raw/gbfs/YYYY/MM/DD/<poll_id>.json.gz`. Nothing is flattened or
   discarded, so a parser bug can be fixed later without losing history.
2. **Indexed** — SQLite `observations.sqlite`: `polls` (one row per fetch),
   `observations` (one row per station per poll, with an eligibility flag and
   an issues list), `errors` (failed fetches), and later `predictions`.
3. **Summarised** — `bikeflow coverage` prints polls, gaps, repeated-source
   count, invalid and stale counts, and errors.

---

## 4. What has been built so far

### Session 1 — data foundation (6 October 2026)

Delivered:

- Python 3.12 + `uv` environment, installable `bikeflow` CLI, test suite.
- GBFS discovery + collector with raw snapshots, SQLite index, TTL-aware
  polling, stale/invalid detection, repeated-source detection, and explicit
  failure logging (a failed poll records an error, never fake data).
- Full February 2026 trip audit: 1,219,444 rows across both CSV parts,
  0 invalid timestamps, 0 negative durations, 1,119 missing start station ids,
  8,058 missing end station ids.
- Station mapping between trip ids and live GBFS ids using `short_name` or
  `station_id`, verified by name and coordinates within 150 m:
  **2,130 matched**, 74 currently inactive, 71 unmatched, 2 renamed.
- A diagnostic 30-minute arrival/departure table for the 20 busiest verified
  stations (observed bins only, not a training grid).
- Initial hosted collection (later moved to the VPS and removed from GitHub,
  then replaced by the VPS scheduled task).

Unresolved issues recorded at the time: missing station ids, month-boundary
spillover, ride-id duplication, station lifecycle, and the need for continuous
collection.

### Session 2 — targets and baseline (14 October 2026)

Delivered:

- Three months of trips downloaded with SHA-256 per archive.
- `bikeflow session2`: streaming scan of every CSV part, NYC→UTC conversion
  with DST-ambiguous timestamps dropped, matched-ID filtering, aggregation
  into 30-minute and 60-minute windows, and partitioned Parquet output
  (`year=/month=/`).
  - 9,150,816 rows at 30 minutes
  - 4,575,408 rows at 60 minutes
  - 2,130 stations
- An explicit eligibility rule: a station is assumed operating from its first
  to its last observed trip day. Windows inside that span are emitted and
  empty ones are genuine zeros. Windows outside are absent — unevaluable,
  never silently zero. Documented in `docs/eligibility.md`.
- Baseline v0: for each `(station, NYC weekday, time slot)`, the average number
  of departures/arrivals. Scored on the final 14 days:
  - 30 min MAE 0.485 departures / 0.479 arrivals
  - 60 min MAE 0.821 departures / 0.811 arrivals
- Two real bugs found by measurement: tz information dropped during grouping
  (all-zero Parquet), and a 28-minute per-station baseline loop (vectorised to
  seconds). Both fixed with regression tests.

### Sprint 1 — models and API (21 October 2026)

This is the current milestone.

---

## 5. Sprint 1 in detail

### 5.1 Goal

Turn the data foundation into a working vertical slice:

> Given the latest live station state, predict departures and arrivals for the
> next 30 and 60 minutes, compare that against a baseline, expose it through an
> API, and save every prediction before its outcome exists.

### 5.2 Training data and split

Input: the Session 2 Parquet targets. One row = one station in one window.

Chronological split, with no shuffling:

| Period | Dates | Use |
| --- | --- | --- |
| Train | before 2 Feb 2026 | Fit the model |
| Validation | 2–15 Feb 2026 | Early stopping |
| Test | 16–28 Feb 2026 | One-shot evaluation |

The test period is never used for tuning. It is the same boundary used by
baseline v0, so the comparison is on identical rows.

### 5.3 Features

The model intentionally uses **calendar-only** features:

- `station_code` — categorical id, fit on training rows only.
- `weekday`, `hour`, `minute_slot` — in `America/New_York`.
- `is_weekend`.
- `day_of_year` — captures the seasonal ramp.

Why no lag features yet? Lag features need a reliable history of past
observations. The live archive only has days, not months, so lags would be
zero-filled in training and impossible to reproduce at serving time. Shipping
a train/serve mismatch is worse than shipping fewer features. Lags return when
the live history supports them.

Why no weather or holidays yet? They are planned but belong to the next sprint
of data work. Adding them on three winter months with one holiday would not be
measurable. This is stated as a limitation, not hidden.

### 5.4 Models

Four pooled LightGBM regressors:

```
30min departures    30min arrivals
60min departures    60min arrivals
```

"Pooled" means one model serves all stations, using `station_code` as a
categorical feature — not one model per station. This shares statistical
strength across quiet stations and keeps the artifact small.

Training settings: 600 trees max, learning rate 0.05, 31 leaves, early
stopping after 50 rounds without validation improvement. Best iteration was
72–75, so early stopping did the work.

Artifacts in `data/models/v1/`:

- four booster files (`30min_departures.txt`, etc.);
- `metadata.json` — dataset hash, feature list, hyperparameters, and the
  station-code mapping bridged to 2,130 live GBFS ids;
- `metrics.json` — every number below.

### 5.5 Results

Measured on 1,396,464 test windows at 30 min and 698,232 at 60 min, per flow:

| Horizon | Flow | Model MAE | Baseline MAE | Improvement | Busy-50 model | Busy-50 baseline |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 30 min | departures | 0.412 | 0.478 | +13.8% | 1.81 | 1.93 |
| 30 min | arrivals | 0.408 | 0.472 | +13.4% | 1.80 | 1.89 |
| 60 min | departures | 0.746 | 0.811 | +8.0% | 3.29 | 3.42 |
| 60 min | arrivals | 0.740 | 0.801 | +7.5% | 3.28 | 3.36 |

Reading the numbers: MAE is the average absolute error in trips per station per
window. Lower is better. The model beats the baseline on every horizon, every
flow, and on the 50 busiest stations — the ones that matter most operationally.

Context: the global average is 0.52 departures per station per 30 minutes, with
two thirds of windows at zero. An MAE of 0.412 is therefore a meaningful
improvement on a sparse target, not a rounding effect.

### 5.6 Live serving

FastAPI runs on the VPS and exposes:

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Status, model version, dataset hash |
| `GET /freshness` | Snapshot age and stale flag |
| `GET /stations` | Latest eligible stations with bikes/docks/disabled |
| `GET /forecasts/{station_id}?horizon=30\|60` | Forecast, expected state, warning score, recommendation, snapshot provenance |
| `GET /queue?horizon=&limit=` | Stations ranked by warning score |
| `GET /predictions` | Persisted prediction log |

Serving logic:

1. Read the newest collector snapshot from SQLite (cached 30 s to avoid
   re-parsing 2,519 payloads per request).
2. Encode the station using the mapping stored in `metadata.json`. Live GBFS ids
   are UUIDs or legacy numeric ids; trip data uses short ids. The bridge was a
   real bug found by testing — before it, every station fell into the "unknown"
   bucket and received the same prediction.
3. Predict departures and arrivals for the next window.
4. Combine with current state:
   `expected_bikes = bikes + arrivals − departures`.
5. Score: shortage (fewer than 3 bikes expected) plus overflow (fewer than 3
   docks expected), with a small activity tie-breaker.
6. Recommend `add_bikes`, `remove_bikes`, `monitor`, or `unavailable`.
   Recommendations respect physics: you cannot add bikes to a station with no
   usable docks, and stations with 0 bikes and 0 docks (typically all assets
   disabled) are excluded from the queue as not actionable.
7. Persist the prediction before the target window with issue time, target
   window, horizon, station, model version, snapshot poll id, flows, score, and
   recommendation.

Stale data is surfaced, not hidden: `/freshness` reports the age, and every
forecast carries `snapshot_stale`.

### 5.7 Why the API is reached through a tunnel

The API binds to `127.0.0.1` on the VPS. It is not publicly reachable. The Mac
opens:

```
ssh -f -N -L 8000:127.0.0.1:8000 bikeflow
```

which forwards `localhost:8000` on the Mac to port 8000 on the VPS over SSH.
This keeps the unauthenticated prototype off the public internet. JWT,
rate limiting, input validation, and public deployment are Sprint 3/4 work.

On Windows, use `127.0.0.1` rather than `localhost` in the tunnel: the latter
resolves to IPv6 first while uvicorn binds IPv4, which produces empty replies.

### 5.8 What Sprint 1 deliberately did not do

- **12-month history** — the pipeline is proven on three months; expanding is
  bandwidth and time, not new code.
- **PostgreSQL** — SQLite is enough for a single-writer prototype. Postgres is
  planned for Sprint 3/4 with predictions and evaluation tables.
- **Weather join** — planned, with a strict rule to use only forecast runs
  available at prediction time.
- **Poisson baseline** — only worth adding if it informs the comparison; the
  LightGBM already beats v0.
- **Dashboard** — Sprint 2.
- **Docker, CI/CD, cloud deployment, JWT** — Sprint 4, required by the teacher.

---

## 6. How the pieces connect during a demo

1. The VPS collector fetches Citi Bike status every minute and stores it.
2. On the Mac, a tunnel exposes the VPS API locally.
3. `/freshness` confirms the snapshot is seconds old.
4. `/queue` asks the API to score every eligible station and returns the most
   urgent ones.
5. `/forecasts/{id}` returns a single station's 30- and 60-minute outlook.
6. `/predictions` proves each of those forecasts was written down before its
   target window.
7. `metrics.json` and `reports/sprint1/REPORT.md` show the measured accuracy
   against the baseline.

---

## 7. Repository map

```
src/bikeflow/
  gbfs.py        feed discovery, HTTP fetch, validation, timestamps
  collector.py   raw snapshots, SQLite index, coverage, failure logging
  trips.py       monthly ZIP audit, station mapping, diagnostics
  session2.py    Parquet targets, eligibility spans, baseline v0
  model.py       feature engineering, LightGBM training, comparison
  api.py         FastAPI app, scoring, queue, prediction log
  cli.py         command line entry points
tests/
  test_data_foundation.py   collector, mapping, audit, DST
  test_session2.py          targets, eligibility, no-leakage baseline
  test_sprint1.py           features, artifacts, API, prediction ordering
docs/
  eligibility.md            the three eligibility rules
ops/
  vps-collector.md          VPS setup, tasks, tunnel, retention
reports/
  sprint1/                  report, metrics, verification commands
data/                       ignored: raw snapshots, SQLite, Parquet, models
```

---

## 8. Roadmap

**Sprint 2 — integration and frontend.** React dashboard with station map,
normal/warning/critical states, both horizons, ranked queue, station detail,
data-freshness indicators, and a recorded replay for demo reliability. Also
scheduled: 12-month data expansion, weather join (no leakage), PostgreSQL.

**Sprint 3 — robustness, security, evaluation.** JWT auth with roles, rate
limiting, validation, explicit fallback to the baseline model, warning-quality
evaluation against later snapshots (precision/recall/lead time/top-K), and
documentation that rebalancing constrains observed demand.

**Sprint 4 — cloud and industrialization.** Docker for API, database, and jobs;
GitHub Actions CI/CD; public deployment; object storage for snapshots and
artifacts; scheduled retraining with MLflow tracking; a promotion gate
demonstrated with both an accepted and a rejected candidate, plus rollback.

**Conditional.** A direct empty/full classifier only if archived history
contains enough failure events. Otherwise the forecast-based score remains, as
the proposal states.

---

## 9. Services and tools explained

### External services

- **Citi Bike GBFS** (General Bikeshare Feed Specification) — public JSON feed.
  Every minute it reports, per station: bikes available, docks available,
  whether the station is installed/renting/returning, and when it last
  reported. This is the only live data source.
- **Citi Bike monthly trip archives** — ZIP files of historical rides hosted on
  Amazon S3 (Simple Storage Service). Each row is a completed trip with start
  and end times, station identifiers, and coordinates. This is the only
  historical demand source.
- **GitHub** — source control and code hosting. Holds source, tests, and docs.
  It never holds raw data, databases, models, or secrets.
- **The VPS** (Virtual Private Server) — a rented Windows machine in a data
  centre that stays on. Runs collection and the API.

### Python tools

- **uv** — installs Python packages and runs commands inside the project
  environment. `uv run` executes in `.venv` so versions match `uv.lock`.
- **pandas** — table processing: reads CSV (Comma-Separated Values) and Parquet,
  groups, joins, converts timestamps. Performs the actual data manipulation.
- **pyarrow** — the engine behind Parquet and pandas. Reads and writes columnar
  files efficiently.
- **numpy** — fast numeric arrays used under pandas and LightGBM.
- **LightGBM** (Light Gradient Boosting Machine) — gradient-boosted decision
  trees. Trains the four forecast models. CPU-only; seconds per model at this
  data size.
- **scikit-learn** — required by LightGBM's scikit-learn wrapper; also provides
  utilities used indirectly.
- **FastAPI** — web framework. Defines endpoints (`/queue`, `/forecasts`),
  validates query parameters, and generates interactive documentation at
  `/docs`.
- **uvicorn** — the web server that runs FastAPI and listens on port 8000.
- **pytest** — test runner. Executes the 16 automated checks.
- **ruff** — linter and formatter. Enforces style and catches unused imports.

### Storage

- **Raw `.json.gz` files** — the original GBFS payload per poll, compressed
  with gzip. Immutable evidence: if parsing changes later, data can be
  reprocessed without re-downloading.
- **SQLite** (`observations.sqlite`) — a single-file relational database with no
  server process. Tables:
  - `polls` — one row per fetch (time, counts, hash, file path, TTL);
  - `observations` — one row per station per poll (eligibility, issues, raw
    payload);
  - `errors` — failed fetches;
  - `predictions` — every forecast served, with issue time, target time, and
    model version.
  Chosen because there is a single writer (the collector) and operations are
  tiny. It will be replaced by PostgreSQL when multiple services need
  concurrent access.
- **Parquet** (`data/processed/session2/`) — compressed columnar files for
  training. 9.2 million rows take about 3.3 MB because zeros compress
  extremely well. Partitioned by `year=/month=/` so one month can be read
  without loading everything.
- **Model `.txt` files** — LightGBM's native model format. Four boosters plus
  `metadata.json` (feature list, dataset hash, station mapping) and
  `metrics.json` (measured error results).

### Operating-system pieces on the VPS

- **Task Scheduler** — runs `BikeFlowCollector` and `BikeFlowAPI` at startup,
  restarts them on failure, and keeps them alive after RDP (Remote Desktop
  Protocol) disconnects.
- **OpenSSH Server** — lets the Mac open a remote shell and a port tunnel, using
  key authentication instead of a password.
- **PowerShell / cmd** — command shells used to create tasks, inspect logs, and
  run commands.

### Networking

- **SSH** (Secure Shell) **tunnel** —
  `ssh -L 8000:127.0.0.1:8000` forwards the Mac's `localhost:8000` to the VPS
  loopback port. The API is never exposed to the public internet.
- **HTTP** (Hypertext Transfer Protocol) and **JSON** (JavaScript Object
  Notation) — the protocol and payload format between the collector and Citi
  Bike, and between a browser and FastAPI.

### Not used yet

- PostgreSQL, Docker, GitHub Actions (CI/CD), MLflow, Airflow, React dashboards
  — planned for Sprints 2 to 4.
- No paid API, no GPU, no Kubernetes, no Kafka.

---

## 10. Glossary and abbreviations

- **GBFS** — General Bikeshare Feed Specification; the public JSON standard for
  bike-share station state.
- **Poll / snapshot** — one fetch of all station states at one moment.
- **Eligible (live)** — installed, renting, returning, valid counts, and fresh
  timestamps. Ineligible rows stay archived but never drive decisions.
- **Window** — a fixed 30- or 60-minute interval that predictions and targets
  refer to, aligned to UTC boundaries.
- **Target** — the observed number of departures or arrivals in a window.
- **Baseline** — the historical average by station, weekday, and slot.
- **MAE** — mean absolute error, in trips per station per window.
- **Pooled model** — one model for all stations, with station id as a feature.
- **Warning score** — a transparent priority number combining expected
  shortage, expected overflow, and activity. Not a probability.
- **Artifact** — a trained model file plus its metadata, versioned so a
  prediction can always be traced to the exact model and dataset.

### Abbreviations

- **API** — Application Programming Interface; here, the HTTP endpoints that
  return JSON.
- **CI/CD** — Continuous Integration / Continuous Deployment; automated testing
  and release pipelines.
- **CSV** — Comma-Separated Values, a plain-text table format.
- **DST** — Daylight Saving Time; clocks shift by one hour twice a year, which
  creates ambiguous or nonexistent local timestamps.
- **GBFS** — General Bikeshare Feed Specification, the public JSON standard for
  bike-share station state.
- **HTTP/HTTPS** — Hypertext Transfer Protocol (Secure), the web protocol.
- **JSON** — JavaScript Object Notation, a text format for structured data.
- **JWT** — JSON Web Token, a signed token used for authentication.
- **MAE** — Mean Absolute Error, the average absolute difference between
  predictions and observations, in trips per station per window.
- **NYC** — New York City.
- **RDP** — Remote Desktop Protocol, used to log into the Windows VPS.
- **S3** — Amazon Simple Storage Service, object storage hosting the trip
  archives.
- **SSH** — Secure Shell, an encrypted remote login and tunnelling protocol.
- **TTL** — Time To Live, how long a feed considers its data valid.
- **UTC** — Coordinated Universal Time, the timezone used for all stored
  instants.
- **VPS** — Virtual Private Server, a rented always-on machine.
- **ZIP** — a compressed archive file format; the monthly trip archives contain
  CSV files inside a ZIP.
