"""Sprint 1: pooled LightGBM flow models, model-vs-baseline comparison, artifacts.

Local-only. Reads the Session 2 Parquet targets, trains arrivals/departures
models for the 30 and 60 minute horizons, and writes versioned artifacts under
data/models/<version>/ (gitignored).

Leakage rules:
- Chronological split only: train < VAL_START, validation < TEST_START,
  untouched test from TEST_START. TEST_START matches baseline v0 (final 14 days).
- Lag features are strict past values (same station, same wall-clock slot 1 day
  and 7 days earlier), loaded from the full table but only from timestamps
  before the row's own window.
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
    return frame.sort_values(["station_id", "window_start_utc"]).reset_index(drop=True)


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


def train_one(horizon, flow, frame, val_start, test_start, params, version_dir):
    target = flow
    train = frame[frame.window_start_utc < pd.Timestamp(val_start, tz="UTC")].copy()
    val = frame[
        (frame.window_start_utc >= pd.Timestamp(val_start, tz="UTC"))
        & (frame.window_start_utc < pd.Timestamp(test_start, tz="UTC"))
    ].copy()
    test = frame[frame.window_start_utc >= pd.Timestamp(test_start, tz="UTC")].copy()
    log(f"Model {horizon}min {flow}: train {len(train):,} | val {len(val):,} | test {len(test):,}")
    mapping, unknown = encode_stations(train, [val, test])
    features = feature_columns()
    booster = lgb.LGBMRegressor(**params)
    booster.fit(
        train[features],
        train[target],
        eval_set=[(val[features], val[target])],
        eval_metric="l1",
        categorical_feature=["station_code"],
        callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(0)],
    )
    log(f"Model {horizon}min {flow}: best_iteration {booster.best_iteration_}")
    return booster, test, mapping, unknown


def baseline_means_for_test(frame, horizon, test_start):
    """Baseline v0 means from the same pre-test history the model trains on.

    Delegates to Session 2's train_key_means so the comparison is
    apples-to-apples: identical rows, identical eligibility arithmetic.
    """
    events = frame.rename(columns={"departures": "dep", "arrivals": "arr"})
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
    for flow, key in (("departures", "dep"), ("arrivals", "arr")):
        events_frame = (
            events.groupby(["station_id", "window_start_utc"], observed=True)[key]
            .sum()
            .reset_index()
        )
        events_frame.columns = ["station", "window", "n"]
        result[flow] = train_key_means(events_frame, span_map, test_start, horizon)
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
        dataset_hash.update(
            pd.util.hash_pandas_object(
                frame[["station_id", "window_start_utc", "departures", "arrivals"]], index=False
            ).values.tobytes()
        )
        last = frame.window_start_utc.max()
        test_start = (last - timedelta(days=TEST_DAYS - 1)).date().isoformat()
        val_start = (pd.Timestamp(test_start) - timedelta(days=VAL_DAYS)).date().isoformat()
        log(f"Model {horizon}min: val_start {val_start}, test_start {test_start}")
        baselines, spans = baseline_means_for_test(frame, horizon, test_start)
        horizon_metrics = {"val_start": val_start, "test_start": test_start, "flows": {}}
        for flow in FLOWS:
            log(f"Model {horizon}min {flow}: training")
            booster, test, mapping, unknown = train_one(
                horizon, flow, frame, val_start, test_start, params, version_dir
            )
            model_path = version_dir / f"{horizon}min_{flow}.txt"
            booster.booster_.save_model(str(model_path))
            station_mapping.update({k: int(v) for k, v in mapping.items()})
            bridged = {
                live: int(mapping[trip]) for live, trip in live_to_trip.items() if trip in mapping
            }
            station_mapping.update(bridged)
            log(f"Model {horizon}min {flow}: bridged {len(bridged):,} live ids")
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
                frame[frame.window_start_utc < pd.Timestamp(test_start, tz="UTC")]
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
