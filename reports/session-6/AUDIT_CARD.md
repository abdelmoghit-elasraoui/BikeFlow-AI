# Session 6 — live audit card

Guest evaluator: Pr. Hatim LECHGAR (EHTP; ex-IBM IT Architect & ML Specialist).
Review scope: **the AI/ML core only**. Date: 19 November 2026.

Keep this open on the laptop. Every command is one line and copy-pasteable.

---

## 0. Ten minutes before he joins

```sh
ssh -f -N -L 8000:127.0.0.1:8000 bikeflow; sleep 2; curl -s localhost:8000/health; echo; curl -s localhost:8000/freshness
```

```sh
cd ~/Desktop/bikeflow && .venv/bin/python -m pytest -q 2>&1 | tail -2
```

```sh
ls -lh data/models/v1
```

Want: `"status":"ok"`, `"stale":false`, `16 passed`, four boosters of ~400KB.
If the first call returns nothing, the SSH tunnel died — rerun the first line.
The services on the server are unaffected.

---

## 1. Opening — 30 seconds

> "BikeFlow forecasts short-horizon bike availability per station for Citi Bike
> NYC. We predict departures and arrivals separately for the next 30 and 60
> minutes, combine them with live inventory, and rank the stations that will run
> short or overflow. It is a pooled LightGBM trained on 5.1M trips, evaluated
> against a seasonal-naive baseline on 1.4M held-out windows it never saw.
> CPU-only, fully local, and every forecast is logged before its target window."

---

## 2. Demo sequence

### 2.1 Live system — three clocks (2 min)

```sh
curl -s localhost:8000/freshness | python3 -m json.tool
```

```sh
curl -s "localhost:8000/queue?horizon=30&limit=5" | python3 -c "import json,sys; d=json.load(sys.stdin); [print(f\"{r['station_id'][:12]}  dep={r['forecast_departures']:.2f} arr={r['forecast_arrivals']:.2f} bikes={r['current_bikes']} docks={r['current_docks']} -> {r['recommendation']}\") for r in d['queue']]"
```

```sh
SID=$(curl -s "localhost:8000/queue?horizon=30&limit=1" | python3 -c "import json,sys; print(json.load(sys.stdin)['queue'][0]['station_id'])"); curl -s "localhost:8000/forecasts/$SID?horizon=30" | python3 -m json.tool
```

Say: the forecast targets the **next full 30-minute window**, not "now". The
snapshot is seconds old and `snapshot_stale` is explicit, so stale input is
visible rather than silent. `warning_score` is a transparent priority
heuristic — expected shortage + overflow + a small flow term — **not a
calibrated probability**.

### 2.2 Data — provenance (2 min)

```sh
python3 -c "import json; m=json.load(open('data/processed/session2/manifest.json')); [print(x['archive'], 'rows', x['counts']['rows'], 'kept_out', x['counts']['unmatched_kept_out'], 'stations_seen', x['matched_stations_seen']) for x in m['months']]; print('total_stations', m['stations'], 'test_start', m['test_start'])"
```

Say: 5,127,455 trips over three months, 2,130 stations verified against the
GBFS↔trip ID mapping. ~719k trips were **dropped rather than guessed** —
unmatched station IDs are excluded. Windows outside a station's observed
lifespan are absent, not zero, so we never train on fictional zeros.

### 2.3 Leakage defence — the part that matters (3 min)

```sh
grep -n "train = frame\|val = frame\|test = frame\|encode_stations(train\|early_stopping\|No lag features" src/bikeflow/model.py
```

Say, in this order: chronological only (train < 2 Feb, validation 2–15 Feb,
test 16–28 Feb untouched); validation used only for early stopping; station
encoding fit on training rows only; unseen live IDs map to one reserved code;
**no lag features**, because with five days of archive a lag would be a
train-only artifact serving could not reproduce.

### 2.4 Results — the headline table (3 min)

```sh
python3 -c "import json; d=json.load(open('reports/sprint1/model_vs_baseline.json')); [print(h, f, 'model', round(x['model_mae'],3), 'baseline', round(x['baseline_mae'],3), f\"+{x['improvement_pct']:.1f}%\", 'busy50', round(x['busy50_model_mae'],2), 'vs', round(x['busy50_baseline_mae'],2)) for h,v in d['horizons'].items() for f,x in v['flows'].items()]"
```

Say: the baseline is a seasonal-naive — station × weekday × time-of-day
historical mean fit on the **same pre-test history**. It already knows the
weekly shape, so beating it is non-trivial. The model wins on every horizon,
every flow, and on the 50 busiest stations, so the gain is not regression to
the mean. Best iteration 72–75 of 600: early stopping is doing real work.

### 2.5 Reproducibility (2 min)

```sh
python3 -c "import json; m=json.load(open('data/models/v1/metadata.json')); print('hash', m['dataset_hash'][:16], 'features', m['feature_columns'], 'codes', len(m['station_codes']))"
```

```sh
.venv/bin/python -c "import lightgbm as lgb; b=lgb.Booster(model_file='data/models/v1/30min_departures.txt'); print('trees', b.num_trees(), 'features', b.num_feature(), b.feature_name())"
```

```sh
.venv/bin/python -m pytest -q 2>&1 | tail -2
```

Say: the dataset hash is stored inside the artifact, so any model traces back
to the exact frame that produced it. 4,234 codes = 2,117 trip-station
categories + 2,117 live GBFS bridges. Sixteen tests cover the API contract,
prediction logging, and training invariants — including `issued < target`.

---

## 3. Numbers to know cold

| Fact | Value |
| --- | ---: |
| Raw trips processed | 5,127,455 |
| Verified stations | 2,130 |
| Trips excluded (unmatched IDs) | 719,058 |
| Target rows, 30 min / 60 min | 9,150,816 / 4,575,408 |
| Held-out test windows per flow | 1,396,464 / 698,232 |
| Split boundary | train < 2 Feb · val 2–15 Feb · test 16–28 Feb 2026 |
| MAE 30 min dep / arr | 0.412 / 0.408 (baseline 0.478 / 0.472) |
| MAE 60 min dep / arr | 0.746 / 0.740 (baseline 0.811 / 0.801) |
| Improvement | +13.8% / +13.4% · +8.0% / +7.5% |
| Best iteration | 72–75 of 600 |
| Live stations / polls / raw | 2,519 / 19.4k / 3.8GB |

---

## 4. "How did you build it?" — the 90-second answer

> "Six stages, each with an artifact that can be checked."

**1. Collect.** `bikeflow collect` polls the Citi Bike GBFS discovery,
`station_information` and `station_status` every 60 seconds. Each poll is
written as a gzipped raw snapshot (`/data/gbfs/YYYY/MM/DD/<poll_id>.json.gz`)
and indexed by one row in SQLite. Raw is the source of truth; the database is
only an index. Failures go to an `errors` table, never fabricated as data.

**2. Audit trips.** `bikeflow audit-trips` opens each monthly S3 ZIP, validates
the schema across every CSV part, counts rows, checks duplicate ride IDs, and
records SHA-256 per file.

**3. Map identifiers.** Trip archives use short numeric station IDs; GBFS uses
UUIDs. The mapping is built by matching `short_name`, name and coordinates, and
each station is labelled `matched`, `unmatched` or `missing`. 2,130 matched.
Nothing is guessed; unmatched IDs are excluded and counted.

**4. Build targets.** `bikeflow session2` scans each month in 100k-row chunks,
floors trip start/end times to 30- and 60-minute UTC windows, and counts
departures by start station and arrivals by end station. Eligibility heuristic:
a station operates between its first and last observed trip day — inside that
span empty windows are genuine zeros, outside it rows are absent. Output is
partitioned Parquet (`year=/month=/`) plus a manifest of checksums and counts.

**5. Baseline.** Mean trips per (station, weekday, slot) over eligible training
windows, with a global-mean fallback for unseen keys (0.4%). Same test rows as
the model, so the comparison is apples-to-apples. 0.478 / 0.472 at 30 minutes.

**6. Train and evaluate.** Calendar features in `America/New_York` — weekday,
hour, minute slot, weekend, day of year — plus `station_code` as a categorical.
Four pooled LightGBM regressors (2 horizons × 2 flows), 600 trees max, learning
rate 0.05, 31 leaves, early stopping after 50 rounds. Test is predicted once.
Output: four booster files plus `metadata.json` (dataset hash, features,
hyperparameters, station map) and `metrics.json`.

**Serving.** `bikeflow serve` loads the four models, reads the newest raw
snapshot, builds one feature frame for all eligible stations at the next window
boundary, batch-predicts, clamps negatives to zero, and combines with live
inventory: `expected_bikes = bikes + arrivals − departures`. Every response —
and every queue row — is written to `predictions` before its target window.

**Commands, in order:**

```
bikeflow audit-trips <zip> --snapshot <gbfs.json>
bikeflow session2 --months 202512 202601 202602
bikeflow train --version v1
bikeflow serve --version v1
```

---

## 5. Where we are, and what is next

| Sprint | Milestone | State |
| --- | --- | --- |
| Sprint 1 — business logic and AI | **Session 6 (today)** | **20 cards Done.** Scope change recorded: 3 months, calendar-only features |
| Sprint 2 — integration and frontend | Session 7 | 11 cards Todo |
| Sprint 3 — robustness, security, evaluation | Session 9 | Planned |
| Sprint 4 — cloud and industrialization | Sessions 10–11 | Planned |

**Sprint 2 backlog, in the order it will be done:**

1. **12-month trips + retrain `v2`.** Highest-value ML item: removes the
   winter-only limitation and makes the test window non-winter.
2. **Weather join.** Cached, using archived forecast runs available at
   prediction time — no leakage of later observations.
3. **React dashboard.** Station map, normal/warning/critical states, both
   horizons, ranked queue, station detail, freshness, and a recorded replay so
   the demo survives a feed outage.
4. **PostgreSQL behind a storage abstraction.** SQLite→Postgres becomes a
   configuration change, not a rewrite.
5. **Predictions retention/dedup.** `/queue` logs ~2,500 rows per call.

**Sprint 3** adds JWT with roles, rate limits, input validation, an explicit
fallback to the baseline when the model cannot run, and warning-quality
evaluation (precision, recall, lead time, top-K capture against later
snapshots). **Sprint 4** adds Docker, GitHub Actions, object storage, scheduled
retraining with MLflow, drift checks, and a promotion gate demonstrated with
both an accepted and a rejected candidate.

---

## 6. Questions he will ask

| Question | Answer |
| --- | --- |
| Why LightGBM, not a neural net or ARIMA? | 2,100 categorical stations, tabular count target. GBDT is the strongest default there, trains in minutes on CPU, and is explainable. Per-station ARIMA means 2,100 models and no cold start; a global deep model needs far more engineering to beat GBDT on this shape. |
| Why pooled rather than per-station? | Per-station series are sparse; pooling shares the weekly shape, and `station_code` learns station-level offsets. It also handles cold start: 13 stations unseen in training fall back to the reserved unknown code. |
| Why MAE, not RMSE or MAPE? | The decision is "how many bikes short" — a count error in bikes. MAE is robust to the rush-hour tail; RMSE would be dominated by a few windows. MAPE is undefined because most windows are 0 or 1. |
| What loss did you optimise? | LightGBM's default L2 objective, with **L1 early stopping and L1 reporting** because MAE is the decision metric. Matching the objective to L1 is a candidate experiment, not something I claim we did. |
| Is +13% real? | Against a seasonal-naive that already knows station, weekday and time-of-day, over 1.4M held-out windows, and it holds on the busiest 50 stations. Real but modest; the headroom is weather, holidays and lags. |
| Overfitting? | Early stopping on a 14-day validation block, best iteration 72–75 of 600, test touched once, station encoding fit on train only. |
| Is `warning_score` calibrated? | No, and the report says so. It ranks; it is not a probability. Calibration only matters once we move to expected-cost decisions. |
| How would you detect drift? | Every forecast is stored with its target window, so once trips land we can join and compute rolling MAE. Prediction distribution and station-set changes are cheap early signals. The logging exists; the scorer is Sprint 2/3. |
| Why 30 and 60 minutes? | 30 minutes is actionable for dispatching a van; 60 minutes is planning. Beyond that, calendar-only features dominate and error grows. |
| Why not predict inventory directly? | Inventory is bounded by capacity and moved by staff; flows plus current state is more stable, interpretable, and capacity-agnostic across stations. |
| Is this AI or statistics? | Supervised learning — gradient-boosted trees, not an LLM, deliberately. Tabular count forecasting is where GBDT still wins. Name the decision it improves: which 20 stations to rebalance next. |

---

## 7. The weakness — say it before he finds it

> "One limitation worth flagging: v1 is trained on December–February. In
> November `day_of_year` is outside the trained range, so the tree clamps it to
> the late-February regime. The weekly shape is still right; the seasonal level
> is not. That is exactly why Sprint 2 retrains on twelve months before we claim
> anything about summer or fall. The pipeline is season-agnostic — this artifact
> is not."

Said first, it is rigour. Said after he finds it, it is an excuse. The same
applies to: winter-only test window, no weather, approximated station lifespans,
and an uncalibrated queue score. All four are already written in
`reports/sprint1/REPORT.md` — keep that file open in a second tab.

---

## 8. Do not

- Run training live. It takes hours; nothing good happens on a projector.
- Claim production readiness, security, or a dashboard. None exist yet.
- Mention the London or Windows hosts, or that password. Irrelevant to the ML
  core and it invites a tangent.
- Say "accuracy". You have MAE, and you know why.
- Quote the 13% without naming the baseline — the first follow-up will be
  "what is the baseline?".
- Call `/queue` twice in a row if avoidable: each call logs ~2,500 prediction
  rows.
