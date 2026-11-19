"""Sprint 1: pooled LightGBM flow models, model-vs-baseline comparison, artifacts.

Local-only. Reads the Session 2 Parquet targets, trains arrivals/departures
models for the 30 and 60 minute horizons, and writes versioned artifacts under
data/models/<version>/ (gitignored).

Leakage rules:
- Chronological split only: train < VAL_START, validation < TEST_START,
  untouched test from TEST_START. TEST_START matches baseline v0 (final 14 days).
- No lag features: only ~5 days of archived snapshots existed when v1 was
  trained, so a lag would be a train-only artifact serving could not
  reproduce. They return once the raw archive is long enough.
- Preprocessing (station category mapping) is fit on training rows only.
"""

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from .gbfs import utc_now
from .session2 import train_key_means

HORIZONS = (30, 60)
FLOWS = ("departures", "arrivals")
VAL_DAYS = 14
TEST_DAYS = 14
PROGRESS_EVERY = 1_000_000
_STARTED = time.monotonic()


def log(message):
    elapsed = time.monotonic() - _STARTED
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{now} UTC +{elapsed:7.1f}s] {message}", flush=True)


def load_targets(out_dir, horizon):
    path = Path(out_dir) / f"targets_{horizon}min"
    frame = pd.read_parquet(
        path, columns=["station_id", "window_start_utc", "departures", "arrivals"]
    )
    frame["station_id"] = frame.station_id.astype("string")
    # No sort: the split is chronological by mask, every aggregation is
    # order-independent, and Parquet read order is already deterministic. At
    # 13 months a station-string sort copies ~40M rows for no benefit.
    return frame.reset_index(drop=True)


def add_features(frame):
    """Calendar features derived from UTC instants; no future information."""
    log(f"Features: computing calendar columns for {len(frame):,} rows")
    ts = pd.DatetimeIndex(frame.window_start_utc)
    local = ts.tz_convert("America/New_York")
    frame["weekday"] = local.weekday.astype("int8")
    frame["hour"] = local.hour.astype("int8")
    frame["minute_slot"] = (local.hour * 60 + local.minute).astype("int16")
    frame["is_weekend"] = (local.weekday >= 5).astype("int8")
    frame["day_of_year"] = local.dayofyear.astype("int16")
    return frame


def feature_columns():
    return [
        "station_code",
        "weekday",
        "hour",
        "minute_slot",
        "is_weekend",
        "day_of_year",
    ]


def encode_stations(train, others):
    """Fit station categorical codes on train rows only."""
    categories = pd.Index(sorted(train.station_id.unique()))
    mapping = {station: i for i, station in enumerate(categories)}
    unknown = len(mapping)
    for frame in [train, *others]:
        frame["station_code"] = frame.station_id.map(mapping).fillna(unknown).astype("int32")
    return mapping, unknown


def utc_naive(values):
    """Naive UTC numpy datetimes; tz-aware Series.to_numpy() returns objects."""
    index = pd.DatetimeIndex(values)
    if index.tz is not None:
        index = index.tz_convert("UTC").tz_localize(None)
    return index.to_numpy()


def train_one(horizon, flow, frame, val_start, test_start, params):
    """Fit one pooled model, slicing features instead of copying the frame.

    The 13-month build holds ~38M rows; three full-frame copies (train, val,
    test) plus LightGBM's own conversion do not fit in 8GB of RAM.
    """
    features = feature_columns()
    val_ts = pd.Timestamp(val_start).to_datetime64()
    test_ts = pd.Timestamp(test_start).to_datetime64()
    windows = utc_naive(frame.window_start_utc)
    train_mask = windows < val_ts
    val_mask = (windows >= val_ts) & (windows < test_ts)
    test_mask = windows >= test_ts
    log(
        f"Model {horizon}min {flow}: train {int(train_mask.sum()):,} | "
        f"val {int(val_mask.sum()):,} | test {int(test_mask.sum()):,}"
    )
    booster = lgb.LGBMRegressor(**params)
    booster.fit(
        frame.loc[train_mask, features],
        frame.loc[train_mask, flow],
        eval_set=[(frame.loc[val_mask, features], frame.loc[val_mask, flow])],
        eval_metric="l1",
        categorical_feature=["station_code"],
        callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(0)],
    )
    log(f"Model {horizon}min {flow}: best_iteration {booster.best_iteration_}")
    test = frame.loc[test_mask, ["station_id", *features, flow]].copy()
    return booster, test


def baseline_means_for_test(frame, horizon, test_start):
    """Baseline v0 means from the same pre-test history the model trains on.

    Delegates to Session 2's train_key_means so the comparison is
    apples-to-apples: identical rows, identical eligibility arithmetic.
    The targets frame already holds one row per (station, window), so the
    old groupby-sum was a no-op that copied the whole frame; columns are
    passed by name instead.
    """
    spans_frame = (
        frame.groupby("station_id", observed=True)
        .agg(first=("window_start_utc", "min"), last=("window_start_utc", "max"))
        .reset_index()
    )
    span_map = {
        row.station_id: (row.first.date().isoformat(), row.last.date().isoformat())
        for row in spans_frame.itertuples()
    }
    result = {}
    for flow, key in (("departures", "departures"), ("arrivals", "arrivals")):
        result[flow] = train_key_means(
            frame,
            span_map,
            test_start,
            horizon,
            columns=("station_id", "window_start_utc", key),
        )
        log(f"Baseline {horizon}min {flow}: {len(result[flow]):,} keys")
    return result, span_map


def mae(predictions, truth):
    return float(
        np.abs(np.asarray(predictions, dtype="float64") - np.asarray(truth, dtype="float64")).mean()
    )


def run_training(
    out_dir,
    models_dir,
    version,
    mapping_path=Path("reports/first-session/station_mapping.csv"),
    months=("202512", "202601", "202602"),
):
    """Train 4 models, compare against baseline v0, write artifacts + metrics."""
    out_dir, models_dir = Path(out_dir), Path(models_dir)
    version_dir = models_dir / version
    version_dir.mkdir(parents=True, exist_ok=True)
    live_to_trip = {}
    mapping_path = Path(mapping_path)
    if mapping_path.exists():
        mapping = pd.read_csv(mapping_path, dtype="string")
        matched = mapping[mapping.mapping_status == "matched"]
        live_to_trip = {
            str(live): str(trip)
            for live, trip in zip(matched.gbfs_station_id, matched.trip_station_id)
            if isinstance(live, str) and isinstance(trip, str)
        }
        log(f"Mapping: {len(live_to_trip):,} live GBFS ids bridged to trip ids")
    else:
        log(f"Mapping: {mapping_path} missing; live ids will fall back to unknown codes")
    log(f"Training run starting: version {version}, months {months}")
    params = {
        "n_estimators": 600,
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_child_samples": 40,
        "subsample": 0.9,
        "subsample_freq": 1,
        "colsample_bytree": 0.9,
        "reg_lambda": 1.0,
        "random_state": 42,
        "n_jobs": 4,
        "verbose": -1,
    }
    metrics = {"generated_at": utc_now(), "version": version, "horizons": {}}
    dataset_hash = hashlib.sha256()
    station_mapping = {}
    for horizon in HORIZONS:
        frame = load_targets(out_dir, horizon)
        frame = add_features(frame)
        log(f"Model {horizon}min: dataset hash update")
        # Per column: hashing the 4-column frame at once built a ~1GB copy of
        # the 13-month table just to fingerprint it.
        for column in ("station_id", "window_start_utc", "departures", "arrivals"):
            dataset_hash.update(
                pd.util.hash_pandas_object(frame[column], index=False).values.tobytes()
            )
        last = frame.window_start_utc.max()
        test_start = (last - timedelta(days=TEST_DAYS - 1)).date().isoformat()
        val_start = (pd.Timestamp(test_start) - timedelta(days=VAL_DAYS)).date().isoformat()
        log(f"Model {horizon}min: val_start {val_start}, test_start {test_start}")
        baselines, _ = baseline_means_for_test(frame, horizon, test_start)
        train_mask = (
            utc_naive(frame.window_start_utc) < pd.Timestamp(val_start).to_datetime64()
        )
        log(f"Model {horizon}min: fitting station codes on {int(train_mask.sum()):,} train rows")
        # Encoding depends only on the station column, so it is fitted once per
        # horizon instead of once per flow.
        mapping, unknown = encode_stations(
            frame.loc[train_mask, ["station_id"]], [frame]
        )
        station_mapping.update({k: int(v) for k, v in mapping.items()})
        bridged = {
            live: int(mapping[trip]) for live, trip in live_to_trip.items() if trip in mapping
        }
        station_mapping.update(bridged)
        log(
            f"Model {horizon}min: {len(mapping):,} station codes, "
            f"{len(bridged):,} live ids bridged"
        )
        horizon_metrics = {"val_start": val_start, "test_start": test_start, "flows": {}}
        for flow in FLOWS:
            log(f"Model {horizon}min {flow}: training")
            booster, test = train_one(horizon, flow, frame, val_start, test_start, params)
            model_path = version_dir / f"{horizon}min_{flow}.txt"
            booster.booster_.save_model(str(model_path))
            features = feature_columns()
            model_pred = booster.predict(test[features])
            base_pred = pd.Series(
                [
                    baselines[flow].get((str(s), int(wd), int(slot)), 0.0)
                    for s, wd, slot in zip(
                        test.station_id,
                        test.weekday,
                        (test.minute_slot // 30 if horizon == 30 else test.hour),
                    )
                ]
            ).values
            truth = test[flow].values
            model_mae = mae(model_pred, truth)
            base_mae = mae(base_pred, truth)
            busy_ids = (
                frame.loc[
                    utc_naive(frame.window_start_utc) < pd.Timestamp(test_start).to_datetime64(),
                    ["station_id", flow],
                ]
                .groupby("station_id", observed=True)[flow]
                .sum()
                .nlargest(50)
                .index
            )
            busy_mask = test.station_id.isin(busy_ids).values
            log(
                f"Model {horizon}min {flow}: MAE model {model_mae:.3f} vs baseline {base_mae:.3f} "
                f"({len(test):,} test windows)"
            )
            horizon_metrics["flows"][flow] = {
                "model_mae": model_mae,
                "baseline_mae": base_mae,
                "improvement_pct": (base_mae - model_mae) / base_mae * 100 if base_mae else None,
                "busy50_model_mae": mae(model_pred[busy_mask], truth[busy_mask]),
                "busy50_baseline_mae": mae(base_pred[busy_mask], truth[busy_mask]),
                "test_windows": int(len(test)),
                "best_iteration": int(booster.best_iteration_ or params["n_estimators"]),
                "station_categories": len(mapping),
                "unknown_station_code": int(unknown),
                "model_file": model_path.name,
            }
        metrics["horizons"][f"{horizon}min"] = horizon_metrics
        # Free this horizon before the next load; keeping both frames alive
        # doubles peak memory at 13-month scale.
        del frame, baselines
    metadata = {
        "version": version,
        "generated_at": utc_now(),
        "dataset_hash": dataset_hash.hexdigest(),
        "months": list(months),
        "targets_dir": str(out_dir),
        "feature_columns": feature_columns(),
        "station_codes": station_mapping,
        "params": params,
        "val_days": VAL_DAYS,
        "test_days": TEST_DAYS,
    }
    (version_dir / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (version_dir / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    log(f"Artifacts written to {version_dir}")
    return {"metadata": metadata, "metrics": metrics}
