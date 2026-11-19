# Sprint 2 — 13-month trip history and model `v2`

Completed 19 November 2026. Scope: extend the trip history from three months to
thirteen, rebuild the 30- and 60-minute targets, retrain the four pooled
LightGBM models as `v2`, evaluate them against the same historical baseline, and
deploy the result. `v1` and `data/processed/session2/` are untouched, so the
Sprint 1 evidence stays reproducible.

## What shipped

- Thirteen monthly trip archives, October 2025 to October 2026: 50,472,470
  trips, each archive verified byte-exact against S3.
- Targets rebuilt at both horizons in `data/processed/v2/`: 40,314,384 rows at
  30 minutes and 20,157,192 at 60 minutes, 2,130 stations.
- Four `v2` boosters plus per-key offset tables in `data/models/v2/`.
- The L1 objective and the additive offset described below.
- `v2` serving on the Sydney host, with `/health`, `/queue`, and `/forecasts`
  verified against the live snapshot.

## Data added

All thirteen archives were verified byte-exact against S3 and scanned before
the build. The schema is identical across 2025 and 2026.

| Month | Rows | Month | Rows |
| --- | ---: | --- | ---: |
| 2025-10 | 4,731,900 | 2026-05 | 4,694,878 |
| 2025-11 | 3,417,052 | 2026-06 | 5,384,468 |
| 2025-12 | 2,091,620 | 2026-07 | 4,993,137 |
| 2026-01 | 1,816,391 | 2026-08 | 5,246,236 |
| 2026-02 | 1,219,444 | 2026-09 | 4,982,855 |
| 2026-03 | 2,945,753 | 2026-10 | 5,088,365 |
| 2026-04 | 3,860,371 | **Total** | **50,472,470** |

The window is thirteen calendar months, not twelve, because the archives are
monthly. October 2026 contributes only its first five days to training, before
the validation boundary; the rest of it is inside the test window. Nothing is
dropped to make the count round.

The station mapping is unchanged: 2,130 stations labelled `matched`, 74
inactive, 71 unmatched, and 2 renamed (`*_review`) excluded. Unmatched station
IDs are excluded rather than guessed.

## Targets and split

The eligibility rule is unchanged from Sprint 1: a station is considered open
between its first and last observed trip day. Inside that span an empty window
is a genuine zero; outside it there is no row. Absent data is never zero-filled.

| Boundary | Date |
| --- | --- |
| Training | before 2026-10-06 |
| Validation (early stopping only) | 2026-10-06 to 2026-10-19 |
| Untouched test | 2026-10-20 to 2026-11-02 |

Baseline v0 is refitted on the same pre-test history: the mean per (station,
weekday, slot), with a global-mean fallback. Fallback rate is 0.0% at both
horizons, so every test key has real history behind it.

## The first `v2` run lost to the baseline

The first thirteen-month run was worse than the baseline on every flow, by
12.6% to 14.7%: 30-minute departures 1.008 against 0.896, arrivals 1.011
against 0.897. Both models also hit the 600-tree cap with validation still
improving, which `v1` had never done (best iteration 72-75).

Three experiments isolated the cause.

| Experiment | Result | Conclusion |
| --- | --- | --- |
| Force `day_of_year` back inside the trained range | 1.032, slightly worse than 1.008 | Extrapolation beyond the training year is not the cause |
| Score an in-range window (September 2026) | 1.060 against 0.941 | The model already loses in range; not a seasonal extrapolation problem |
| Per-key **median** instead of mean | 0.871 against 0.896 at 30 minutes | 2.8% of the loss is the wrong statistic |

The third row is the root cause. **MAE is minimised by the conditional median,
but the model was trained with LightGBM's default L2 objective, which targets
the mean, and then scored with L1.** The baseline had the same mismatch, but it
was smaller: a per-key mean is a worse estimate of the per-key median when the
count distribution is skewed toward zero.

The longer history also explains why `v1` looked better. With three winter
months each (station, weekday, slot) key had about 8 samples; with thirteen
months it has about 58. The baseline the model has to beat became substantially
stronger, while the model still had six calendar features and 31 leaves per
tree, roughly 18,600 leaves against 715,680 distinct keys.

## The fix: L1 objective plus a per-key offset

Two changes, both evaluated on the same held-out window.

1. **`regression_l1` objective.** The model now predicts the conditional
   median, which is the statistic MAE scores.
2. **Additive per-key offset.** Targets are centred on the mean per (station,
   weekday, slot) computed strictly from rows before the validation boundary.
   The model learns corrections — season, holidays, interactions — instead of
   spending its leaves re-deriving a lookup table it can be handed directly.
   The table ships as `{horizon}min_{flow}_base.npy` and the API adds it back;
   the saved booster alone predicts the correction.

The offset was verified independently before training: 715,680 keys, test MAE
0.9013, correctly conservative against a true test mean of 1.479.

## Results

Identical held-out windows for model and baseline, 1,294,896 test windows at 30
minutes and 647,448 at 60.

| Horizon | Flow | Model MAE | Baseline MAE | Improvement | Busy-50 model | Busy-50 baseline |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 30 min | departures | 0.879 | 0.896 | **+1.8%** | 3.18 | 3.26 |
| 30 min | arrivals | 0.883 | 0.897 | **+1.6%** | 3.23 | 3.27 |
| 60 min | departures | 1.471 | 1.468 | −0.2% | 5.81 | 5.74 |
| 60 min | arrivals | 1.481 | 1.476 | −0.3% | 5.86 | 5.80 |

**The 30-minute models now beat the baseline on both flows and on the 50
busiest stations. The 60-minute models do not** — they are 0.2-0.3% worse,
which is a tie at this sample size but is reported as a loss.

That is not an implementation failure, and the size of the remaining headroom
measures it. The per-key median, which is the best any calendar-only lookup can
do at this aggregation, offers:

| Horizon | Per-key mean MAE | Per-key median MAE | Headroom |
| --- | ---: | ---: | ---: |
| 30 min | 0.8957 | 0.8708 | 2.79% |
| 60 min | 1.4681 | 1.4604 | 0.53% |

At 60 minutes the typical station-hour has a median of one trip and 46% of
windows are zero, so station, weekday and hour already explain almost all of
the calendar signal. What remains is weather, events and day-to-day variation
that calendar features cannot see. The model's best iteration there is 61 of
600: early stopping fires because there is nothing left to learn, not because
the model is unstable.

## Where the gain comes from

Slicing the same 30-minute test rows (`reports/sprint2/error_slices_30min.json`):

| Slice | Windows | Model | Baseline | Improvement |
| --- | ---: | ---: | ---: | ---: |
| All | 1,294,896 | 0.879 | 0.896 | +1.8% |
| Busiest 10% of stations | 128,448 | 2.490 | 2.508 | +0.7% |
| Other 90% | 1,166,448 | 0.702 | 0.718 | +2.3% |
| Weekends | 373,658 | 0.897 | 0.926 | +3.2% |
| Weekdays | 921,238 | 0.872 | 0.883 | +1.3% |
| 01:00-04:00 | — | — | — | +11% to +12% |
| 15:00-18:00 | — | — | — | −0.3% to +0.2% |

Arrivals show the same shape: +1.6% overall, +3.1% on weekends, −0.15% on the
busiest stations, and no gain between 16:00 and 18:00.

The model is better on 58.6% of windows — a real but modest edge, and it is
concentrated where the baseline is weakest: low-volume hours and quiet
stations. **At the afternoon peak, which is when a dispatcher actually needs
the forecast, the two are indistinguishable.** That is the honest limit of
calendar-only features, and it is the strongest argument for weather and live
state: both vary within a weekday, so both act exactly where the calendar model
has nothing left to say.

## Comparison with `v1`

The two versions are not comparable head to head — different test windows and
different baselines.

| Version | Months | Test window | 30 min dep/arr | 60 min dep/arr |
| --- | ---: | --- | ---: | ---: |
| `v1` | 3 (Dec-Feb) | 16-28 Feb 2026 | +13.8% / +13.4% | +8.0% / +7.5% |
| `v2` | 13 | 20 Oct-2 Nov 2026 | +1.8% / +1.6% | −0.2% / −0.3% |

The `v1` margin was flattered by a weakly estimated baseline and a winter test
window. The `v2` margin is small because the baseline is strong, and it is
measured on a season the model was not trained on.

## Two serving bugs the deployment exposed

1. **Unknown station code.** The API used `len(station_codes)` as the unseen
   category, which was 4,260. `station_codes` also carries live-ID bridges, so
   the trained category count is 2,130. Unseen stations were being scored as a
   category that does not exist. Metadata now records `unknown_station_code`
   explicitly.
2. **Offset table bounds.** The table was sized by `max(station_code)`, but
   serving maps unseen stations to `max + 1`, so `/queue` returned HTTP 500 with
   `index 715835 is out of bounds`. The table now covers the unseen category and
   fills unseen keys with the global training mean. Loading also pads short
   tables, which repairs artifacts written before the bound existed.

Both are covered by tests, including one that truncates the table to the shape
that failed on the server.

## Cost and scale

| Stage | Wall clock | Peak resident memory |
| --- | ---: | ---: |
| Targets, 13 months | 5.5 min | — |
| Training, four models | 20.0 min | 2.9 GB |

Training runs on the 8 GB laptop with 8 cores; the server only serves. The
memory work is what makes 40M rows fit: features are added in place, splits are
boolean masks over one frame instead of three copies, station encoding is done
per horizon and released before the next load, and the dataset hash is updated
per column.

## Live-state correction: trained, measured, disabled

The additive correction is implemented — `src/bikeflow/live.py`, the
`live-features` and `train-live` commands, and a serving path that loads a
correction only when it is enabled. The migrated snapshot archive was found on
the secondary disk (`/data/gbfs`), merged into the live archive, and the overlap
with the trip archive turned out to be 16–24 October: 871,170 labelled rows at
30 minutes and 434,520 at 60.

On its chronological holdout it looks excellent:

| Horizon | Flow | Baseline | Calendar | Blended | Change |
| --- | --- | ---: | ---: | ---: | ---: |
| 30 min | departures | 0.892 | 0.871 | 0.804 | +7.8% |
| 30 min | arrivals | 0.894 | 0.874 | 0.799 | +8.5% |
| 60 min | departures | 1.416 | 1.407 | 1.237 | +12.1% |
| 60 min | arrivals | 1.424 | 1.415 | 1.237 | +12.6% |

Every validation window improved. That is too uniform to be state information,
so two checks were run before enabling it.

**Ablation (30-minute departures).** Training on the forecast level alone
(`calendar`, station, hour) gives +7.1%; training on live state without the
forecast level gives +4.0% on 74% of windows, and much of that is still a
station/hour level term. The gain is mostly a recalibration of the calendar
model, not the state.

**Direction check.** Flows were inferred from consecutive snapshots — a one-bike
step is a trip, multi-bike jumps are rebalancing — and calibrated on October,
where the proxy captures 67.9% of labelled departures. Applied to 12–19 November:

| Estimate | Trips per window | vs estimated truth |
| --- | ---: | ---: |
| Snapshot proxy × calibration | 0.846 | — |
| Calendar model | 0.986 | +16.5% |
| Blended | 1.113 | +31.5% |

The calendar model under-predicts October by 30.6% and over-predicts November by
16.5%: late October sits above the annual mean and November below it. The
correction learned the October direction, so **it would make November worse**.
The four artifacts are deployed but disabled in `live_metrics.json`, with that
reason recorded in the file. The November trip archive provides the labels that
make the correction transferable; until then the gate stays closed.

This is the promotion gate doing its job, and it is the argument for evaluating
on rolling windows rather than one seasonal block.

**Archive recovery.** The migrated snapshots were on `/data`, a separate disk
that the first audit did not scan. After merging: 29,412 of 29,437 indexed polls
have their raw file. 112 polls from the 24 October disk-full outage are
truncated past repair; the feature builder falls back to the previous intact
poll and reports how many it skipped.

## Limitations

- **60 minutes has no measured calendar headroom.** The tie is reported, not
  hidden. Weather is the next feature that can change it.
- **The offset is the per-key mean, not the median.** With an L1 objective the
  optimal per-key constant is the median, worth up to 2.8% at 30 minutes. Left
  as a measured next step rather than changed at the end of the sprint.
- **The 30-minute models stop at the 600-tree cap** with validation still
  improving, so they are capacity-limited. Raising the cap with early stopping
  on validation is the next experiment.
- **One 14-day test window, no confidence interval.** The improvement is small
  enough that a paired bootstrap would be more informative than a point MAE.
- **Calendar-only features.** No weather, holidays, events, or lag features.
  `v1` documented why lags were excluded; the archive is now long enough to
  reconsider that.
- **The gain is concentrated off-peak.** At the afternoon peak the model and
  the baseline are indistinguishable. Reported in the slice table above, not
  hidden behind the headline MAE.
- **The live-state correction is disabled.** It wins on its October holdout but
  the gain is a seasonal level fix that reverses in November. Reported with the
  ablation and the proxy check, and re-gated when November labels arrive.
- **The baseline is a mean, so it is also mis-specified for MAE.** The
  comparison is fair in construction but neither side is optimal.

## Next steps

1. Weather join from archived forecast runs, cached, with no leakage of later
   observations.
2. Per-key median offset, then re-measure: it bounds what calendar features can
   deliver at 60 minutes.
3. Raise the tree cap for the 30-minute models and stop on validation.
4. Re-run the live-state correction when the November trip archive lands:
   `bikeflow live-features --start 2026-11-12 --end 2026-12-01 --horizon 30 --out data/processed/live_30min.parquet`
   then `bikeflow train-live --live data/processed/live_30min.parquet`. It is
   deployed disabled because the October gain is a seasonal level fix; November
   labels are what make it transferable.
5. Move to rolling-origin backtesting: the current 14-day window contains no
   holiday and cannot resolve a 0.5% difference.
6. Predictions retention and deduplication, then PostgreSQL behind the storage
   abstraction.

## Reproduction

```sh
uv run bikeflow session2 --months 202510 202511 202512 202601 202602 202603 202604 202605 202606 202607 202608 202609 202610
uv run bikeflow train --version v2 --targets data/processed/v2 --months 202510 202511 202512 202601 202602 202603 202604 202605 202606 202607 202608 202609 202610
uv run bikeflow serve --version v2 --port 8000
```

Machine-readable results: `data/models/v2/metrics.json` and
`reports/sprint2/model_vs_baseline.json`. Artifact metadata, including the
dataset hash, feature list, parameters and unknown station code:
`data/models/v2/metadata.json`.
