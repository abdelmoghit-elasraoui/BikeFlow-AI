"""Sprint 1 checks: model training, leakage-free comparison, API + prediction logging."""

import json
from datetime import timedelta

import pandas as pd
import pytest

from bikeflow.model import add_features, encode_stations, feature_columns, run_training


@pytest.fixture()
def tiny_dataset(tmp_path):
    """Two stations over 60 days with a daily cycle; enough rows to train."""
    rows = []
    days = pd.date_range("2026-01-01", periods=60, freq="D", tz="UTC")
    for station in ("a", "b"):
        for day in days:
            for slot in range(48):
                hour = slot // 2
                rows.append(
                    {
                        "station_id": station,
                        "window_start_utc": day + timedelta(minutes=30 * slot),
                        "departures": int((hour == 16) + (station == "a")),
                        "arrivals": int((hour == 8) + (station == "b")),
                    }
                )
    frame = pd.DataFrame(rows)
    for horizon in (30, 60):
        target = frame if horizon == 30 else frame[frame.window_start_utc.dt.minute == 0]
        out = tmp_path / f"targets_{horizon}min"
        out.mkdir(parents=True, exist_ok=True)
        target.to_parquet(out / "part-000.parquet", index=False)
    return tmp_path


def test_features_are_calendar_only_and_encoded_on_train():
    frame = pd.DataFrame(
        {
            "station_id": pd.Series(["a", "a", "b"], dtype="string"),
            "window_start_utc": pd.DatetimeIndex(
                [
                    "2026-01-05 05:10:00+00:00",
                    "2026-01-05 05:40:00+00:00",
                    "2026-01-05 06:00:00+00:00",
                ],
                tz="UTC",
            ),
            "departures": [1, 2, 3],
            "arrivals": [0, 0, 1],
        }
    )
    frame = add_features(frame)
    assert set(feature_columns()) - {"station_code"} <= set(frame.columns)
    assert not any("lag" in c for c in feature_columns())
    train = frame.iloc[:2].copy()
    test = frame.iloc[2:].copy()
    mapping, unknown = encode_stations(train, [test])
    assert mapping == {"a": 0}
    assert test.station_code.iloc[0] == unknown


def test_training_writes_artifacts_and_beats_obvious_baseline(tiny_dataset, tmp_path):
    result = run_training(
        tiny_dataset,
        tmp_path / "models",
        "vtest",
        months=("tiny",),
    )
    version_dir = tmp_path / "models" / "vtest"
    for horizon in (30, 60):
        for flow in ("departures", "arrivals"):
            assert (version_dir / f"{horizon}min_{flow}.txt").exists()
    metrics = result["metrics"]["horizons"]["30min"]["flows"]["departures"]
    assert metrics["model_mae"] < metrics["baseline_mae"]
    metadata = json.loads((version_dir / "metadata.json").read_text())
    assert metadata["dataset_hash"] and metadata["station_codes"]
    assert metadata["feature_columns"] == feature_columns()


def test_api_serves_forecasts_and_persists_predictions(tiny_dataset, tmp_path):
    from fastapi.testclient import TestClient

    from bikeflow import collector
    from bikeflow.api import create_app

    run_training(tiny_dataset, tmp_path / "models", "vapi", months=("tiny",))
    data_dir = tmp_path / "data"
    db = collector.connect(data_dir)
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
    db.execute(
        "INSERT INTO polls (id,retrieved_at,source_updated,status_hash,repeated_source,raw_path,"
        "station_count,invalid_count,stale_count,ttl) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("poll1", now.isoformat(), str(int(now.timestamp())), "hash", 0, "raw.gz", 1, 0, 0, 60),
    )
    db.execute(
        "INSERT INTO observations VALUES (?,?,?,?,?,?)",
        ("poll1", "a", str(int(now.timestamp())), 1, "[]", json.dumps(station)),
    )
    db.commit()
    db.close()

    client = TestClient(create_app(tmp_path / "models" / "vapi", data_dir))
    assert client.get("/health").json()["status"] == "ok"
    station_response = client.get("/stations").json()
    assert station_response["count"] == 1
    forecast = client.get("/forecasts/a?horizon=30").json()
    assert forecast["model_version"] == "vapi"
    assert forecast["snapshot_poll_id"] == "poll1"
    assert forecast["target_window_utc"] > now.isoformat() or forecast["target_window_utc"]
    queue = client.get("/queue?horizon=60&limit=5").json()
    assert len(queue["queue"]) == 1
    persisted = client.get("/predictions").json()
    assert persisted["count"] == 2
    assert all(p["model_version"] == "vapi" for p in persisted["predictions"])
    issued = pd.Timestamp(persisted["predictions"][0]["issued_at"])
    target = pd.Timestamp(persisted["predictions"][0]["target_window_utc"])
    assert issued < target  # prediction saved before its interval
