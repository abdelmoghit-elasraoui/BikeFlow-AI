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
from datetime import timedelta
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from .features import add_calendar
from .features import build as features_build
from .features import feature_columns as features_columns
from .gbfs import utc_now
from .progress import log
from .session2 import train_key_means

HORIZONS = (30, 60)
FLOWS = ("departures", "arrivals")
VAL_DAYS = 14
TEST_DAYS = 14
PROGRESS_EVERY = 1_000_000


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
    """Calendar columns; kept as a wrapper so older callers keep working."""
    return add_calendar(frame)


def feature_columns(include_weather=False):
    """Column list for the original calendar-only models and the weather build."""
    groups = ["calendar", "weather"] if include_weather else ["calendar"]
    return features_columns(groups)


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


def per_row_key_mean(frame, horizon, cutoff, flow):
    """(station, weekday, slot) mean from rows before cutoff, as a flat array.

    Integer key arithmetic instead of a MultiIndex: a 40M-row tuple index
    costs gigabytes, while ``station_code * 336 + weekday * 48 + slot`` fits
    bincount in a few megabytes. Used as the additive offset the model fits
    a correction to, so it does not spend its leaves re-learning a lookup
    table it can be handed directly.
    """
    stations = int(frame.station_code.max()) + 2  # +1 unseen code, +1 inclusive
    width = stations * 7 * 48
    slot = (
        frame.minute_slot.to_numpy(np.int64) // 30
        if horizon == 30
        else frame.hour.to_numpy(np.int64)
    )
    key = frame.station_code.to_numpy(np.int64) * 336 + frame.weekday.to_numpy(np.int64) * 48 + slot
    mask = utc_naive(frame.window_start_utc) < pd.Timestamp(cutoff).to_datetime64()
    values = frame[flow].to_numpy(np.float64)
    totals = np.bincount(key[mask], weights=values[mask], minlength=width)
    counts = np.bincount(key[mask], minlength=width).astype(np.float64)
    overall = float(values[mask].mean())
    # Unseen keys fall back to the global mean, in the table as well: serving
    # reads the table directly, so a zero there would silently drop the level.
    means = np.divide(totals, counts, out=np.full(width, overall), where=counts > 0)
    base = means[key]
    return base.astype(np.float32), means.astype(np.float32)


def train_one(horizon, flow, frame, val_start, test_start, params, base, features):
    """Fit one pooled model on the residual around the per-key mean.

    The 13-month build holds ~38M rows; three full-frame copies (train, val,
    test) plus LightGBM's own conversion do not fit in 8GB of RAM. Targets are
    centred on ``base`` so the model learns corrections (season, holidays)
    rather than reproducing a 715k-key lookup table with 31 leaves per tree.
    """
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
        frame.loc[train_mask, flow].to_numpy() - base[train_mask],
        eval_set=[
            (
                frame.loc[val_mask, features],
                frame.loc[val_mask, flow].to_numpy() - base[val_mask],
            )
        ],
        eval_metric="l1",
        categorical_feature=["station_code"],
        callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(0)],
    )
    log(f"Model {horizon}min {flow}: best_iteration {booster.best_iteration_}")
    test = frame.loc[test_mask, ["station_id", *features, flow]].copy()
    test["base"] = base[test_mask]
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
    weather=False,
    weather_cache=Path("data/raw/weather"),
    feature_groups=None,
    trees=600,
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
    if feature_groups is None:
        feature_groups = ["calendar", "weather"] if weather else ["calendar"]
    feature_groups = tuple(feature_groups)
    log(
        f"Training run starting: version {version}, months {months}, "
        f"groups {list(feature_groups)}, trees {trees}"
    )
    weather_digest = None
    station_context_meta = None
    params = {
        "n_estimators": trees,
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
        # MAE is minimised by the conditional median, not the mean. Training
        # with L2 and scoring with L1 capped v2 at the per-key mean baseline
        # (0.896) while the per-key median reaches 0.871 on the same rows.
        "objective": "regression_l1",
    }
    metrics = {"generated_at": utc_now(), "version": version, "horizons": {}}
    dataset_hash = hashlib.sha256()
    station_mapping = {}
    unknown_code = 0
    for horizon in HORIZONS:
        frame = load_targets(out_dir, horizon)
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
        train_mask = utc_naive(frame.window_start_utc) < pd.Timestamp(val_start).to_datetime64()
        log(f"Model {horizon}min: fitting station codes on {int(train_mask.sum()):,} train rows")
        # Encoding depends only on the station column, so it is fitted once per
        # horizon instead of once per flow.
        mapping, unknown = encode_stations(frame.loc[train_mask, ["station_id"]], [frame])
        station_mapping.update({k: int(v) for k, v in mapping.items()})
        bridged = {
            live: int(mapping[trip]) for live, trip in live_to_trip.items() if trip in mapping
        }
        unknown_code = int(unknown)
        station_mapping.update(bridged)
        log(
            f"Model {horizon}min: {len(mapping):,} station codes, {len(bridged):,} live ids bridged"
        )
        weather_frame = None
        if "weather" in feature_groups:
            from .weather import load as load_weather

            start = str(frame.window_start_utc.min().date())
            end = str((frame.window_start_utc.max() + pd.Timedelta(days=1)).date())
            weather_frame = load_weather(start, end, weather_cache)
            if weather_digest is None:
                weather_digest = hashlib.sha256(
                    pd.util.hash_pandas_object(weather_frame, index=False).values.tobytes()
                ).hexdigest()
        context = None
        if "station" in feature_groups:
            from .features import station_context

            context = station_context(mapping_path)
            context["station_code"] = context.station_id.map(mapping)
            context = context.dropna(subset=["station_code"])
            context["station_code"] = context.station_code.astype("int32")
            if station_context_meta is None:
                station_context_meta = {
                    str(int(row.station_code)): [
                        round(float(row.latitude), 5),
                        round(float(row.longitude), 5),
                        int(row.neighbours_500m),
                    ]
                    for row in context.itertuples()
                }
        frame = features_build(frame, feature_groups, weather=weather_frame, context=context)
        feature_names = features_columns(feature_groups)
        del weather_frame
        log(f"Model {horizon}min: {len(feature_names)} features")
        horizon_metrics = {"val_start": val_start, "test_start": test_start, "flows": {}}
        for flow in FLOWS:
            log(f"Model {horizon}min {flow}: training")
            # Offset uses train rows only, so val and test see a mean the
            # model never fitted on. The table ships with the booster: the
            # saved model predicts the correction, not the level.
            base, key_means = per_row_key_mean(frame, horizon, val_start, flow)
            np.save(version_dir / f"{horizon}min_{flow}_base.npy", key_means)
            booster, test = train_one(
                horizon, flow, frame, val_start, test_start, params, base, feature_names
            )
            del base, key_means
            model_path = version_dir / f"{horizon}min_{flow}.txt"
            booster.booster_.save_model(str(model_path))
            model_pred = booster.predict(test[feature_names]) + test.base.to_numpy()
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
        "feature_columns": feature_names,
        "feature_groups": list(feature_groups),
        "station_context": station_context_meta,
        "weather": (
            {
                "source": "open-meteo historical forecast",
                "variables": feature_names[len(features_columns(["calendar"])) :],
                "cache": str(weather_cache),
                "digest": weather_digest,
            }
            if weather
            else None
        ),
        "station_codes": station_mapping,
        "unknown_station_code": int(unknown_code),
        "base_offset": "per (station, weekday, slot) train mean, additive",
        "params": params,
        "val_days": VAL_DAYS,
        "test_days": TEST_DAYS,
    }
    (version_dir / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (version_dir / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    log(f"Artifacts written to {version_dir}")
    return {"metadata": metadata, "metrics": metrics}
