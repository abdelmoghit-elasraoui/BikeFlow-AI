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

### Live data: GBFS

Citi Bike publishes a General Bikeshare Feed Specification directory at
`https://gbfs.citibikenyc.com/gbfs/gbfs.json`. The collector discovers feed URLs
at runtime instead of hardcoding hosts, then reads two feeds:

- **station_information** — static-ish metadata: station id, name,
  coordinates, dock capacity.
- **station_status** — dynamic state: bikes available, docks available,
  disabled assets, operational flags, per-station `last_reported` time.

Each poll is stored three ways:

1. **Raw** — the complete discovery + information + status JSON, gzipped, at
   `data/raw/gbfs/YYYY/MM/DD/<poll_id>.json.gz`. Nothing is discarded, so a
   parser bug can be fixed later without losing history.
2. **Indexed** — SQLite `observations.sqlite` with three tables:
   `polls` (one row per fetch), `observations` (one row per station per poll,
   with an eligibility flag and an issues list), and `errors` (failed fetches).
3. **Summarised** — `bikeflow coverage` prints polls, gaps, repeated-source
   count, invalid/stale counts, and errors.

### Historical data: monthly trips

Citi Bike publishes monthly ZIPs where each ZIP contains one or more CSVs of
individual trips: start/end times, station ids and names, coordinates, bike
type, and user type. The project currently uses December 2025, January 2026,
and February 2026: **5,127,455 trips**.

Public trip data excludes staff servicing trips, and it does **not** record
rebalancing operations. That matters for interpretation: inventory changes are
not the same as rider demand.

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

## 9. Glossary

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
