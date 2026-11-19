"""Shared fixtures: a small two-station dataset with a daily cycle."""

from datetime import timedelta

import pandas as pd
import pytest


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


@pytest.fixture()
def tiny_weather(tmp_path):
    """A cached weather directory covering the tiny dataset's months."""
    import numpy as np

    from bikeflow.weather import RAW_FEATURES, WEATHER_FEATURES

    directory = tmp_path / "weather"
    directory.mkdir()
    hours = pd.date_range("2026-01-01", "2026-03-31 23:00", freq="h", tz="UTC")
    frame = pd.DataFrame({"weather_hour": hours})
    values = {
        "temperature_2m": 8.0,
        "apparent_temperature": 7.0,
        "precipitation": 0.0,
        "wind_speed_10m": 9.0,
        "relative_humidity_2m": 65.0,
        "cloud_cover": 30.0,
        "snow_depth": 0.0,
        "visibility": 18000.0,
    }
    for name in RAW_FEATURES:
        frame[name] = np.float32(values[name])
    frame["is_precipitating"] = 0
    frame["is_cold"] = 0
    frame = frame[["weather_hour", *WEATHER_FEATURES]]
    for month in ("202601", "202602", "202603"):
        frame.to_parquet(directory / f"forecast_{month}.parquet", index=False)
    return directory
