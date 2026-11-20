"""Live-state features from archived GBFS snapshots, for the correction model.

The calendar model knows the station, the weekday and the time of day, but not
whether the station is empty right now. That state is visible at prediction
time and it matters: a station with zero bikes cannot record departures, so the
label itself is censored by availability.

The correction model is additive on top of the calendar forecast and is only
promoted when it beats that forecast on a held-out slice of the overlap. The
snapshot archive and the trip archive do not overlap yet (snapshots start
2026-11-12, trips end 2026-10-31), so ``train_live_correction`` refuses to fit
rather than training on nothing: the feature side is built now, and the model
trains the day the next monthly trip archive lands.
"""

from __future__ import annotations

import bisect
import gzip
import json
import re
import zlib
from datetime import datetime, timezone
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from .model import add_features, feature_columns
from .progress import log

POLL_NAME = re.compile(r"^(\d{8}T\d{12})Z-")
LIVE_FEATURES = [
    "bikes",
    "docks",
    "capacity",
    "bike_ratio",
    "dock_ratio",
    "is_renting",
]
# The calendar forecast is a feature, not a target: the correction learns
# whether the level is too high or too low, and the station code lets it learn
# per-station state effects without re-deriving the level.
CORRECTION_FEATURES = [*LIVE_FEATURES, "calendar", "hour", "is_weekend", "station_code"]

MIN_ROWS = 10_000
MIN_WINDOWS = 48


def poll_timestamp(path):
    """UTC timestamp of a poll, taken from its filename."""
    match = POLL_NAME.match(Path(path).name)
    if not match:
        raise ValueError(f"Unrecognised poll filename: {path}")
    return datetime.strptime(match.group(1), "%Y%m%dT%H%M%S%f").replace(tzinfo=timezone.utc)


def list_polls(data_dir, start, end):
    """Poll files in [start, end), sorted by timestamp."""
    root = Path(data_dir) / "raw" / "gbfs"
    first, last = pd.Timestamp(start).date(), pd.Timestamp(end).date()
    polls = []
    for day in pd.date_range(first, last, freq="D", inclusive="left"):
        directory = root / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
        if directory.is_dir():
            polls.extend((poll_timestamp(p), p) for p in directory.glob("*.json.gz"))
    polls.sort(key=lambda item: item[0])
    return polls


def parse_poll(path):
    """Capacity by station, plus the station_status rows, from one raw poll."""
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        bundle = json.load(stream)
    capacity = {
        row["station_id"]: int(row.get("capacity") or 0)
        for row in bundle.get("station_information", {})
        .get("payload", {})
        .get("data", {})
        .get("stations", [])
    }
    rows = bundle["station_status"]["payload"]["data"]["stations"]
    return capacity, rows


def build_live_features(data_dir, start, end, horizon, station_codes):
    """One row per (station code, window): the last snapshot before the window.

    This is the same information the API has at issue time, which is what makes
    the correction trainable and servable from one definition. A truncated poll
    is skipped in favour of the previous intact one: the migrated archive has
    files cut short by the October disk-full outage.
    """
    polls = list_polls(data_dir, start, end)
    if not polls:
        raise FileNotFoundError(
            f"No raw snapshots between {start} and {end} under {data_dir}/raw/gbfs"
        )
    windows = pd.date_range(
        pd.Timestamp(start, tz="UTC"),
        pd.Timestamp(end, tz="UTC"),
        freq=f"{horizon}min",
        inclusive="left",
    )
    times = [timestamp for timestamp, _ in polls]
    chosen = {}
    for window in windows:
        index = bisect.bisect_right(times, window.to_pydatetime()) - 1
        if index >= 0:
            chosen.setdefault(index, []).append(window)
    log(
        f"Live: {len(polls):,} polls, {len(windows):,} windows, "
        f"{len(chosen):,} polls needed for {start}..{end}"
    )
    columns = {
        "station_code": [],
        "window_start_utc": [],
        "bikes": [],
        "docks": [],
        "capacity": [],
        "bike_ratio": [],
        "dock_ratio": [],
        "is_renting": [],
    }
    unusable = 0
    corrupt = 0
    for number, (index, assigned) in enumerate(sorted(chosen.items()), 1):
        capacity, rows = {}, None
        for attempt in range(5):
            candidate = index - attempt
            if candidate < 0:
                break
            try:
                capacity, rows = parse_poll(polls[candidate][1])
                break
            except (OSError, EOFError, ValueError, zlib.error):
                corrupt += 1
        if rows is None:
            log(f"Live: no intact poll for {assigned[0]}, window dropped")
            continue
        for station in rows:
            code = station_codes.get(station["station_id"])
            if code is None:
                unusable += 1
                continue
            bikes = int(station.get("num_bikes_available") or 0)
            docks = int(station.get("num_docks_available") or 0)
            size = capacity.get(station["station_id"]) or 0
            if size <= 0:
                # Closed or capacity-less station: bikes + docks is the only
                # size we can infer, and the ratio is then undefined.
                size = bikes + docks
            ratio = round(bikes / size, 4) if size else 0.0
            dock_ratio = round(docks / size, 4) if size else 0.0
            for window in assigned:
                columns["station_code"].append(code)
                columns["window_start_utc"].append(window)
                columns["bikes"].append(bikes)
                columns["docks"].append(docks)
                columns["capacity"].append(size)
                columns["bike_ratio"].append(ratio)
                columns["dock_ratio"].append(dock_ratio)
                columns["is_renting"].append(int(bool(station.get("is_renting"))))
        if number % 48 == 0:
            log(f"Live: parsed {number:,}/{len(chosen):,} polls, {len(columns['bikes']):,} rows")
    frame = pd.DataFrame(columns)
    frame["station_code"] = frame.station_code.astype("int32")
    for name in ("bikes", "docks", "capacity", "is_renting"):
        frame[name] = frame[name].astype("int32")
    for name in ("bike_ratio", "dock_ratio"):
        frame[name] = frame[name].astype("float32")
    frame["window_start_utc"] = pd.to_datetime(frame.window_start_utc, utc=True)
    log(
        f"Live: {len(frame):,} rows, {frame.station_code.nunique():,} stations, "
        f"{frame.window_start_utc.nunique():,} windows, {unusable:,} unmapped states, "
        f"{corrupt:,} corrupt polls skipped"
    )
    return frame.sort_values(["station_code", "window_start_utc"]).reset_index(drop=True)


def _calendar_prediction(frame, models_dir, horizon, flow):
    """The deployed calendar forecast (booster plus offset) and the offset alone."""
    booster = lgb.Booster(model_file=str(Path(models_dir) / f"{horizon}min_{flow}.txt"))
    table = np.load(Path(models_dir) / f"{horizon}min_{flow}_base.npy")
    slot = (
        frame.minute_slot.to_numpy(np.int64) // 30
        if horizon == 30
        else frame.hour.to_numpy(np.int64)
    )
    key = frame.station_code.to_numpy(np.int64) * 336 + frame.weekday.to_numpy(np.int64) * 48 + slot
    raw = booster.predict(frame[feature_columns()])
    # Unseen stations have no key of their own; the table's tail holds the
    # global mean, and the index is clamped for the same reason.
    base = table[np.minimum(key, len(table) - 1)]
    return raw + base, base


def train_live_correction(
    targets_dir,
    live_path,
    models_dir,
    version,
    horizon,
    flow,
    params=None,
    min_rows=MIN_ROWS,
    min_windows=MIN_WINDOWS,
):
    """Fit the additive correction on the labelled overlap, and gate it.

    Promotion is decided on a chronological holdout inside the overlap: the
    artifact is written either way, but ``enabled`` is only true when the blend
    beats the calendar forecast. Serving reads that flag.
    """
    version_dir = Path(models_dir) / version
    metadata = json.loads((version_dir / "metadata.json").read_text())
    codes = metadata["station_codes"]
    live = pd.read_parquet(live_path)
    targets = pd.read_parquet(
        Path(targets_dir) / f"targets_{horizon}min",
        columns=["station_id", "window_start_utc", flow],
    )
    targets["station_code"] = targets.station_id.map(codes)
    targets = targets.dropna(subset=["station_code"])
    targets["station_code"] = targets.station_code.astype("int32")
    merged = live.merge(
        targets[["station_code", "window_start_utc", flow]],
        on=["station_code", "window_start_utc"],
        how="inner",
    )
    log(
        f"Live {horizon}min {flow}: {len(live):,} feature rows, "
        f"{len(targets):,} target rows, {len(merged):,} labelled overlap"
    )
    if merged.empty:
        raise SystemExit(
            "No labelled overlap: every snapshot window falls outside the trip "
            "archive. The next monthly trip archive creates the overlap; until "
            "then this model cannot be fitted on real targets."
        )
    merged = add_features(merged.copy())
    merged["calendar"], merged["baseline"] = _calendar_prediction(
        merged, version_dir, horizon, flow
    )
    merged = merged.sort_values("window_start_utc").reset_index(drop=True)

    windows = np.sort(merged.window_start_utc.unique())
    if len(windows) < min_windows or len(merged) < min_rows:
        raise SystemExit(
            f"Not enough labelled overlap to fit and hold out: "
            f"{len(merged):,} rows over {len(windows):,} windows "
            f"(need {min_rows:,} rows and {min_windows} windows)."
        )
    validation_start = windows[int(len(windows) * 0.8)]
    train_mask = (merged.window_start_utc < validation_start).to_numpy()
    val_mask = ~train_mask
    log(
        f"Live {horizon}min {flow}: train {int(train_mask.sum()):,} | "
        f"validation {int(val_mask.sum()):,} from {pd.Timestamp(validation_start)}"
    )

    settings = {
        "n_estimators": 400,
        "learning_rate": 0.05,
        "num_leaves": 15,
        "min_child_samples": 50,
        "subsample": 0.9,
        "subsample_freq": 1,
        "colsample_bytree": 0.9,
        "reg_lambda": 1.0,
        "random_state": 42,
        "n_jobs": 4,
        "verbose": -1,
        "objective": "regression_l1",
    }
    if params:
        settings.update(params)
    truth = merged[flow].to_numpy(np.float64)
    calendar = merged.calendar.to_numpy(np.float64)
    baseline = merged.baseline.to_numpy(np.float64)
    booster = lgb.LGBMRegressor(**settings)
    booster.fit(
        merged.loc[train_mask, CORRECTION_FEATURES],
        truth[train_mask] - calendar[train_mask],
        eval_set=[
            (
                merged.loc[val_mask, CORRECTION_FEATURES],
                truth[val_mask] - calendar[val_mask],
            )
        ],
        eval_metric="l1",
        categorical_feature=["station_code"],
        callbacks=[lgb.early_stopping(30, verbose=False), lgb.log_evaluation(0)],
    )
    correction = booster.predict(merged.loc[val_mask, CORRECTION_FEATURES])
    blended = calendar[val_mask] + correction
    per_window = (
        pd.DataFrame(
            {
                "window": merged.loc[val_mask, "window_start_utc"].to_numpy(),
                "calendar_err": np.abs(calendar[val_mask] - truth[val_mask]),
                "blended_err": np.abs(blended - truth[val_mask]),
            }
        )
        .groupby("window")[["calendar_err", "blended_err"]]
        .mean()
    )
    comparison = {
        "rows": int(val_mask.sum()),
        "windows": int(merged.loc[val_mask, "window_start_utc"].nunique()),
        "validation_start": str(pd.Timestamp(validation_start)),
        "mae_baseline": float(np.abs(baseline[val_mask] - truth[val_mask]).mean()),
        "mae_calendar": float(np.abs(calendar[val_mask] - truth[val_mask]).mean()),
        "mae_blended": float(np.abs(blended - truth[val_mask]).mean()),
        "bias_calendar": float((calendar[val_mask] - truth[val_mask]).mean()),
        "bias_blended": float((blended - truth[val_mask]).mean()),
    }
    comparison["improvement_pct"] = (
        (comparison["mae_calendar"] - comparison["mae_blended"]) / comparison["mae_calendar"] * 100
        if comparison["mae_calendar"]
        else None
    )
    comparison["enabled"] = bool(comparison["mae_blended"] < comparison["mae_calendar"])
    comparison["windows_improved_pct"] = float(
        (per_window.blended_err < per_window.calendar_err).mean() * 100
    )
    gain = (
        "n/a" if comparison["improvement_pct"] is None else f"{comparison['improvement_pct']:+.2f}%"
    )
    log(
        f"Live {horizon}min {flow}: MAE baseline {comparison['mae_baseline']:.4f}, "
        f"calendar {comparison['mae_calendar']:.4f} -> blended "
        f"{comparison['mae_blended']:.4f} ({gain}), "
        f"{comparison['windows_improved_pct']:.0f}% of windows better, "
        f"{'enabled' if comparison['enabled'] else 'disabled'}"
    )

    booster.booster_.save_model(str(version_dir / f"live_{horizon}min_{flow}.txt"))
    metrics_path = version_dir / "live_metrics.json"
    metrics = json.loads(metrics_path.read_text()) if metrics_path.exists() else {}
    metrics.setdefault(f"{horizon}min", {})[flow] = {
        "features": CORRECTION_FEATURES,
        "params": settings,
        "train_rows": int(train_mask.sum()),
        "validation": comparison,
        "feature_window": {
            "start": str(live.window_start_utc.min()),
            "end": str(live.window_start_utc.max()),
        },
        "model_file": f"live_{horizon}min_{flow}.txt",
    }
    metrics_path.write_text(json.dumps(metrics, indent=2) + "\n")
    return metrics[f"{horizon}min"][flow]


def serving_features(frame, states):
    """Live columns for a serving batch, from the newest snapshot.

    ``frame`` already carries ``station_code`` and the calendar columns; this
    only adds the state the snapshot reports now. Capacity is attached to each
    state row by the API when it reads the poll.
    """
    bikes, docks, sizes, ratios, dock_ratios, renting = [], [], [], [], [], []
    for station_id in frame.station_id:
        state = states.get(station_id, {})
        available = int(state.get("num_bikes_available") or 0)
        free = int(state.get("num_docks_available") or 0)
        size = int(state.get("capacity") or 0) or available + free
        bikes.append(available)
        docks.append(free)
        sizes.append(size)
        ratios.append(available / size if size else 0.0)
        dock_ratios.append(free / size if size else 0.0)
        renting.append(int(bool(state.get("is_renting"))))
    frame["bikes"] = np.asarray(bikes, dtype="int32")
    frame["docks"] = np.asarray(docks, dtype="int32")
    frame["capacity"] = np.asarray(sizes, dtype="int32")
    frame["bike_ratio"] = np.asarray(ratios, dtype="float32")
    frame["dock_ratio"] = np.asarray(dock_ratios, dtype="float32")
    frame["is_renting"] = np.asarray(renting, dtype="int32")
    return frame
