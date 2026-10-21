"""Sprint 1 API: current stations, forecasts, ranked queue, health/freshness.

Predictions are persisted before their target interval with the model version
and the raw snapshot poll id that supplied live state. Local-only, read-only
against the collector database.

Each request opens its own SQLite connection: the collector writes every minute
and sharing one connection across FastAPI's worker threads caused read hangs.
"""

import json
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import lightgbm as lgb
import pandas as pd
from fastapi import FastAPI, HTTPException, Query

from .gbfs import utc_now
from .model import feature_columns

_state_lock = threading.Lock()
LOW_WATER = 3.0
SNAPSHOT_CACHE_SECONDS = 30


def connect(path):
    db = sqlite3.connect(path, check_same_thread=False)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            issued_at TEXT NOT NULL,
            target_window_utc TEXT NOT NULL,
            horizon_min INTEGER NOT NULL,
            station_id TEXT NOT NULL,
            model_version TEXT NOT NULL,
            snapshot_poll_id TEXT,
            departures REAL NOT NULL,
            arrivals REAL NOT NULL,
            warning_score REAL,
            recommendation TEXT
        );
    """)
    return db


def _read_latest(db_path):
    with closing(sqlite3.connect(db_path, timeout=10)) as db:
        row = db.execute(
            "SELECT id, retrieved_at FROM polls ORDER BY retrieved_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None, None
        poll_id, retrieved = row
        stations = {
            r[0]: json.loads(r[1])
            for r in db.execute(
                "SELECT station_id, payload FROM observations WHERE poll_id = ?", (poll_id,)
            )
        }
    return {"poll_id": poll_id, "retrieved_at": retrieved}, stations


def latest_snapshot(db_path, cache=None):
    """Read the newest snapshot, cached for a short window to skip re-parsing."""
    if cache is None:
        return _read_latest(db_path)
    with _state_lock:
        if cache.get("snapshot") and time.time() - cache.get("at", 0) < SNAPSHOT_CACHE_SECONDS:
            return cache["snapshot"], cache["stations"]
    snapshot, stations = _read_latest(db_path)
    if snapshot:
        with _state_lock:
            cache.update(snapshot=snapshot, stations=stations, at=time.time())
    return snapshot, stations


def load_models(models_dir):
    models = {}
    for horizon in (30, 60):
        for flow in ("departures", "arrivals"):
            path = Path(models_dir) / f"{horizon}min_{flow}.txt"
            if not path.exists():
                raise FileNotFoundError(f"Missing model artifact {path}")
            models[(horizon, flow)] = lgb.Booster(model_file=str(path))
    return models


def build_features(frame, horizon):
    ts = pd.DatetimeIndex(frame.window_start_utc)
    local = ts.tz_convert("America/New_York")
    frame["weekday"] = local.weekday.astype("int8")
    frame["hour"] = local.hour.astype("int8")
    frame["minute_slot"] = (local.hour * 60 + local.minute).astype("int16")
    frame["is_weekend"] = (local.weekday >= 5).astype("int8")
    frame["day_of_year"] = local.dayofyear.astype("int16")
    return frame


def create_app(models_dir, data_dir):
    models_dir = Path(models_dir)
    data_dir = Path(data_dir)
    app = FastAPI(title="BikeFlow AI", version="0.1.0")
    models = load_models(models_dir)
    metadata = json.loads((models_dir / "metadata.json").read_text())
    station_codes = metadata.get("station_codes", {})
    unknown_code = len(station_codes)
    db_path = data_dir / "observations.sqlite"
    with closing(connect(db_path)):
        pass  # create the predictions table if absent
    cache = {}

    def score_batch(station_ids, horizon, snapshot, stations):
        """Batch-predict and persist. Returns one dict per station id."""
        now = pd.Timestamp.now(tz="UTC")
        # Next full window boundary: 11:02 -> 11:30 for 30 min, 12:00 for 60 min.
        target = now.ceil(f"{horizon}min")
        frame = pd.DataFrame(
            {
                "station_id": station_ids,
                "window_start_utc": [target] * len(station_ids),
            }
        )
        frame["station_code"] = [station_codes.get(s, unknown_code) for s in station_ids]
        frame = build_features(frame, horizon)
        predictions = {}
        for flow in ("departures", "arrivals"):
            predictions[flow] = models[(horizon, flow)].predict(frame[feature_columns()])
        issued = utc_now()
        rows = []
        results = []
        stale = (
            pd.Timestamp.now(tz="UTC") - pd.Timestamp(snapshot["retrieved_at"])
        ).total_seconds() > 600
        for i, station_id in enumerate(station_ids):
            # Trips cannot be negative; regression can emit small negatives.
            dep = max(0.0, float(predictions["departures"][i]))
            arr = max(0.0, float(predictions["arrivals"][i]))
            state = stations.get(station_id, {})
            bikes = state.get("num_bikes_available", 0)
            docks = state.get("num_docks_available", 0)
            net = bikes + arr - dep
            expected_bikes = max(0.0, net)
            expected_docks = max(0.0, docks + dep - arr)
            shortage = max(0.0, LOW_WATER - expected_bikes) if docks > 0 else 0.0
            overflow = max(0.0, LOW_WATER - expected_docks) if bikes > 0 else 0.0
            warning = shortage + overflow + 0.1 * (dep + arr)
            if shortage > 0 and shortage >= overflow:
                recommendation = "add_bikes"
            elif overflow > 0:
                recommendation = "remove_bikes"
            elif bikes == 0 and docks == 0:
                recommendation = "unavailable"
            else:
                recommendation = "monitor"
            rows.append(
                (
                    issued,
                    target.isoformat(),
                    horizon,
                    station_id,
                    metadata["version"],
                    snapshot["poll_id"],
                    dep,
                    arr,
                    warning,
                    recommendation,
                )
            )
            results.append(
                {
                    "station_id": station_id,
                    "horizon_min": horizon,
                    "issued_at": issued,
                    "target_window_utc": target.isoformat(),
                    "model_version": metadata["version"],
                    "snapshot_poll_id": snapshot["poll_id"],
                    "snapshot_retrieved_at": snapshot["retrieved_at"],
                    "snapshot_stale": stale,
                    "forecast_departures": dep,
                    "forecast_arrivals": arr,
                    "current_bikes": bikes,
                    "current_docks": docks,
                    "expected_bikes_after": round(expected_bikes, 2),
                    "expected_docks_after": round(expected_docks, 2),
                    "estimated_bikes_after": round(net, 2),
                    "warning_score": warning,
                    "recommendation": recommendation,
                }
            )
        with _state_lock, closing(sqlite3.connect(db_path, timeout=10)) as db:
            db.executemany(
                "INSERT INTO predictions (issued_at,target_window_utc,horizon_min,station_id,"
                "model_version,snapshot_poll_id,departures,arrivals,warning_score,recommendation) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            db.commit()
        return results

    def issue_forecast(station_id, horizon):
        snapshot, stations = latest_snapshot(db_path, cache)
        if snapshot is None:
            raise HTTPException(503, "No collected snapshots available")
        if station_id not in stations:
            raise HTTPException(404, "Unknown station id. Get valid ids from /stations.")
        return score_batch([station_id], horizon, snapshot, stations)[0]

    @app.get("/health")
    def health():
        snapshot, _ = latest_snapshot(db_path, cache)
        return {
            "status": "ok" if snapshot else "degraded",
            "model_version": metadata["version"],
            "dataset_hash": metadata["dataset_hash"],
            "has_snapshot": snapshot is not None,
        }

    @app.get("/freshness")
    def freshness():
        snapshot, stations = latest_snapshot(db_path, cache)
        if snapshot is None:
            raise HTTPException(503, "No snapshots")
        age = (pd.Timestamp.now(tz="UTC") - pd.Timestamp(snapshot["retrieved_at"])).total_seconds()
        return {**snapshot, "age_seconds": age, "stations": len(stations), "stale": age > 600}

    @app.get("/stations")
    def stations(eligible_only: bool = True):
        snapshot, rows = latest_snapshot(db_path, cache)
        if snapshot is None:
            raise HTTPException(503, "No snapshots")
        result = []
        for station_id, row in rows.items():
            if eligible_only and not (
                all(row.get(k) for k in ("is_installed", "is_renting", "is_returning"))
                and (row.get("num_bikes_available", 0) + row.get("num_docks_available", 0)) > 0
            ):
                continue
            result.append(
                {
                    "station_id": station_id,
                    "bikes": row.get("num_bikes_available"),
                    "docks": row.get("num_docks_available"),
                    "disabled": row.get("num_bikes_disabled", 0) + row.get("num_docks_disabled", 0),
                    "last_reported": row.get("last_reported"),
                }
            )
        return {"snapshot": snapshot, "count": len(result), "stations": result}

    @app.get("/forecasts/{station_id}")
    def forecast(station_id: str, horizon: int = Query(30, ge=30, le=60)):
        horizon = 30 if horizon < 45 else 60
        return issue_forecast(station_id, horizon)

    @app.get("/queue")
    def queue(horizon: int = Query(30, ge=30, le=60), limit: int = Query(20, ge=1, le=100)):
        horizon = 30 if horizon < 45 else 60
        snapshot, rows = latest_snapshot(db_path, cache)
        if snapshot is None:
            raise HTTPException(503, "No snapshots")
        candidates = [
            station_id
            for station_id, row in rows.items()
            if all(row.get(k) for k in ("is_installed", "is_renting", "is_returning"))
            and (row.get("num_bikes_available", 0) + row.get("num_docks_available", 0)) > 0
        ]
        if not candidates:
            raise HTTPException(503, "No eligible stations in latest snapshot")
        scored = score_batch(candidates, horizon, snapshot, rows)
        scored.sort(key=lambda item: item["warning_score"], reverse=True)
        return {"snapshot": snapshot, "model_version": metadata["version"], "queue": scored[:limit]}

    @app.get("/predictions")
    def predictions(limit: int = Query(20, ge=1, le=200)):
        with closing(sqlite3.connect(db_path, timeout=10)) as db:
            rows = db.execute(
                "SELECT issued_at,target_window_utc,horizon_min,station_id,model_version,"
                "snapshot_poll_id,departures,arrivals,warning_score,recommendation "
                "FROM predictions ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        columns = [
            "issued_at",
            "target_window_utc",
            "horizon_min",
            "station_id",
            "model_version",
            "snapshot_poll_id",
            "departures",
            "arrivals",
            "warning_score",
            "recommendation",
        ]
        return {"count": len(rows), "predictions": [dict(zip(columns, r)) for r in rows]}

    return app
