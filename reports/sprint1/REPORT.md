# Sprint 1 results — models, comparison, API

Completed 21 October 2026. Scope: pooled LightGBM flow models, honest comparison
against the historical baseline, a local FastAPI serving forecasts and the ranked
queue, and persisted predictions. Local-only artifacts stay under `data/`.

## Training data

3 months (Dec 2025-Feb 2026): 5,127,455 trips, 2,130 verified stations.
Targets: 9,150,816 rows at 30 min and 4,575,408 at 60 min, partitioned Parquet.
Split: train before 2026-02-02, validation 2-15 Feb, untouched test 16-28 Feb
(same boundary as baseline v0).

## Model vs baseline (identical held-out windows)

| Horizon | Flow | Model MAE | Baseline MAE | Improvement | Busy-50 model | Busy-50 baseline |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 30 min | departures | 0.412 | 0.478 | +13.8% | 1.81 | 1.93 |
| 30 min | arrivals | 0.408 | 0.472 | +13.4% | 1.80 | 1.89 |
| 60 min | departures | 0.746 | 0.811 | +8.0% | 3.29 | 3.42 |
| 60 min | arrivals | 0.740 | 0.801 | +7.5% | 3.28 | 3.36 |

The model beats the baseline on every horizon and flow, and on the 50 busiest
stations as well. Test windows: 1,396,464 (30 min) and 698,232 (60 min) per flow.
Best iteration 72-75, so early stopping is active rather than capped.

Features are calendar-only (station code, NYC weekday/hour/slot, weekend,
day-of-year). Lag features were deliberately excluded: only ~5 days of live
snapshots exist, so lags would be train-only artifacts that serving could not
reproduce. They return once archived history supports them.

## Artifacts

`data/models/v1/`: 4 boosters (`30min_departures.txt`, `30min_arrivals.txt`,
`60min_departures.txt`, `60min_arrivals.txt`), `metadata.json` (dataset hash,
2,117 station codes bridged to 2,130 live GBFS ids, features, params) and
`metrics.json` (the table above plus per-flow details).

## API

`uv run bikeflow serve --version v1` exposes:

- `/health` — status, model version, dataset hash.
- `/freshness` — snapshot age and a `stale` flag (no silent stale serving).
- `/stations` — latest eligible stations with bikes/docks/disabled counts.
- `/forecasts/{station_id}?horizon=30|60` — dep/arr forecasts, current state,
  expected state, warning score, recommendation, snapshot provenance.
- `/queue?horizon=&limit=` — stations ranked by warning score; empty-usable
  stations get `add_bikes`, full ones `remove_bikes`, stalls with 0 bikes and
  0 docks are marked `unavailable` and excluded from the queue.
- `/predictions` — the persisted prediction log.

Every forecast call writes a row to the `predictions` table before the target
window, with issue time, target window, horizon, station, model version,
snapshot poll id, flows, score, and recommendation.

## Duplicate ride check

All 5,127,455 ride IDs across the three monthly archives are unique; scanning
the boundary months produced 0 duplicate IDs and 0 extra rows. No dedup filter
was needed for this slice. The check must be repeated when months are added,
since only the month boundaries can overlap.

## Limitations

- Test window is winter only; summer demand and peak magnitudes are unseen.
- Live snapshots used in the demo are the last collected poll and are flagged
  stale when older than 10 minutes.
- Capacity and station lifecycle history are approximated (first/last observed
  trip day), as documented in Session 2.
- Ordering in the queue is a transparent priority score, not a calibrated
  probability.
