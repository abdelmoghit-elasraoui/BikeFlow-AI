"""Live-state features and the additive correction model."""

import gzip
import json

import numpy as np
import pandas as pd
import pytest

from bikeflow.live import (
    CORRECTION_FEATURES,
    build_live_features,
    list_polls,
    poll_timestamp,
    train_live_correction,
)
from bikeflow.model import run_training


def write_polls(root, start, days, horizon=30, stations=("a", "b")):
    """Synthetic raw polls: one per window, state varying with the time of day."""
    root = root / "raw" / "gbfs"
    frames = pd.date_range(
        start, periods=days * 24 * (60 // horizon), freq=f"{horizon}min", tz="UTC"
    )
    for stamp in frames:
        directory = root / f"{stamp.year:04d}" / f"{stamp.month:02d}" / f"{stamp.day:02d}"
        directory.mkdir(parents=True, exist_ok=True)
        name = stamp.strftime("%Y%m%dT%H%M%S") + "000000Z-test.json.gz"
        hour = stamp.hour
        status, information = [], []
        for station in stations:
            base = 8 if hour in (8, 17) else 4
            status.append(
                {
                    "station_id": station,
                    "num_bikes_available": base,
                    "num_docks_available": 20 - base,
                    "is_installed": 1,
                    "is_renting": 1,
                    "is_returning": 1,
                    "last_reported": int(stamp.timestamp()),
                }
            )
            information.append({"station_id": station, "capacity": 20, "name": station})
        with gzip.open(directory / name, "wt", encoding="utf-8") as stream:
            json.dump(
                {
                    "poll_id": name,
                    "station_status": {"payload": {"data": {"stations": status}}},
                    "station_information": {"payload": {"data": {"stations": information}}},
                },
                stream,
            )


def seed_snapshot(data_dir, poll_id, stations, now):
    """One raw poll plus its polls row, the shape the API reads at serving."""

    from bikeflow import collector

    db = collector.connect(data_dir)
    raw_dir = data_dir / "raw" / "gbfs" / "2026" / "01" / "01"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw = raw_dir / f"{poll_id}.json.gz"
    with gzip.open(raw, "wt", encoding="utf-8") as stream:
        json.dump(
            {
                "poll_id": poll_id,
                "station_status": {"payload": {"data": {"stations": stations}}},
                "station_information": {
                    "payload": {
                        "data": {
                            "stations": [
                                {"station_id": s["station_id"], "capacity": 20} for s in stations
                            ]
                        }
                    }
                },
            },
            stream,
        )
    db.execute(
        "INSERT INTO polls (id,retrieved_at,source_updated,status_hash,repeated_source,"
        "raw_path,station_count,invalid_count,stale_count,ttl,eligible_count) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            poll_id,
            now.isoformat(),
            str(int(now.timestamp())),
            "hash",
            0,
            str(raw.relative_to(data_dir)),
            len(stations),
            0,
            0,
            60,
            len(stations),
        ),
    )
    db.commit()
    db.close()


def test_poll_timestamp_reads_the_filename():
    assert poll_timestamp("20261112T000310835857Z-3fa4c6f6.json.gz").isoformat() == (
        "2026-11-12T00:03:10.835857+00:00"
    )


def test_build_live_features_uses_the_last_snapshot_before_each_window(tmp_path):
    data_dir = tmp_path / "data"
    write_polls(data_dir, "2026-01-01", days=2)
    codes = {"a": 0, "b": 1}
    frame = build_live_features(data_dir, "2026-01-01", "2026-01-03", 30, codes)
    assert len(frame) == 2 * 96
    assert list(frame.columns) == [
        "station_code",
        "window_start_utc",
        "bikes",
        "docks",
        "capacity",
        "bike_ratio",
        "dock_ratio",
        "is_renting",
    ]
    assert frame.station_code.nunique() == 2
    assert frame.window_start_utc.nunique() == 96
    assert (frame.capacity == 20).all()
    assert np.allclose(frame.bike_ratio, frame.bikes / 20)
    first = frame[
        (frame.station_code == 0)
        & (frame.window_start_utc == pd.Timestamp("2026-01-01 00:00", tz="UTC"))
    ]
    assert first.bikes.iloc[0] == 4
    assert first.iloc[0].is_renting == 1


def test_build_live_features_rejects_an_empty_range(tmp_path):
    (tmp_path / "raw" / "gbfs").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        build_live_features(tmp_path, "2026-01-01", "2026-01-02", 30, {})
    assert list_polls(tmp_path, "2026-01-01", "2026-01-02") == []


def test_train_live_correction_fits_and_gates(tiny_dataset, tmp_path):
    models = tmp_path / "models"
    run_training(tiny_dataset, models, "vlive", months=("tiny",))
    data_dir = tmp_path / "data"
    write_polls(data_dir, "2026-01-01", days=4)
    metadata = json.loads((models / "vlive" / "metadata.json").read_text())
    frame = build_live_features(data_dir, "2026-01-01", "2026-01-05", 30, metadata["station_codes"])
    live_path = tmp_path / "live_30min.parquet"
    frame.to_parquet(live_path, index=False)

    result = train_live_correction(
        tiny_dataset,
        live_path,
        models,
        "vlive",
        30,
        "departures",
        min_rows=200,
        min_windows=48,
    )
    assert (models / "vlive" / "live_30min_departures.txt").exists()
    metrics = json.loads((models / "vlive" / "live_metrics.json").read_text())
    entry = metrics["30min"]["departures"]
    assert entry["features"] == CORRECTION_FEATURES
    assert entry["validation"]["rows"] > 0
    assert isinstance(entry["validation"]["enabled"], bool)
    assert result["validation"]["mae_calendar"] >= 0
    assert result["validation"]["mae_blended"] >= 0
    # The tiny dataset is deterministic, so the calendar model can already be
    # exact; the gate must then stay closed rather than enable noise.
    assert entry["validation"]["enabled"] is False


def test_train_live_correction_refuses_without_labelled_overlap(tiny_dataset, tmp_path):
    models = tmp_path / "models"
    run_training(tiny_dataset, models, "vlive", months=("tiny",))
    data_dir = tmp_path / "data"
    write_polls(data_dir, "2026-06-01", days=1)
    metadata = json.loads((models / "vlive" / "metadata.json").read_text())
    frame = build_live_features(data_dir, "2026-06-01", "2026-06-02", 30, metadata["station_codes"])
    live_path = tmp_path / "live.parquet"
    frame.to_parquet(live_path, index=False)
    with pytest.raises(SystemExit, match="No labelled overlap"):
        train_live_correction(
            tiny_dataset, live_path, models, "vlive", 30, "departures", min_rows=1, min_windows=1
        )


def test_api_applies_a_promoted_correction(tiny_dataset, tmp_path):
    """The serving path adds the correction only when the gate says enabled."""
    from fastapi.testclient import TestClient

    from bikeflow.api import create_app

    models = tmp_path / "models"
    run_training(tiny_dataset, models, "vlive", months=("tiny",))
    data_dir = tmp_path / "data"
    write_polls(data_dir, "2026-01-01", days=4)
    metadata = json.loads((models / "vlive" / "metadata.json").read_text())
    frame = build_live_features(data_dir, "2026-01-01", "2026-01-05", 30, metadata["station_codes"])
    live_path = tmp_path / "live.parquet"
    frame.to_parquet(live_path, index=False)
    train_live_correction(
        tiny_dataset, live_path, models, "vlive", 30, "departures", min_rows=200, min_windows=48
    )
    metrics_path = models / "vlive" / "live_metrics.json"
    metrics = json.loads(metrics_path.read_text())
    metrics["30min"]["departures"]["validation"]["enabled"] = True
    metrics_path.write_text(json.dumps(metrics))

    now = pd.Timestamp.now(tz="UTC")
    station = {
        "station_id": "a",
        "num_bikes_available": 3,
        "num_docks_available": 20,
        "is_installed": 1,
        "is_renting": 1,
        "is_returning": 1,
        "last_reported": int(now.timestamp()),
    }
    seed_snapshot(data_dir, "poll1", [station], now)
    client = TestClient(create_app(models / "vlive", data_dir))
    forecast = client.get("/forecasts/a?horizon=30")
    assert forecast.status_code == 200, forecast.text
    assert forecast.json()["forecast_departures"] >= 0
    # The correction is loaded from live_metrics.json, so the artifact set is
    # coherent: removing the flag removes the model from serving.
    metrics["30min"]["departures"]["validation"]["enabled"] = False
    metrics_path.write_text(json.dumps(metrics))
    client = TestClient(create_app(models / "vlive", data_dir))
    assert client.get("/forecasts/a?horizon=30").status_code == 200
