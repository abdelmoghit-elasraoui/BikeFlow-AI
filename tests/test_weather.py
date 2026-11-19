"""Weather ingestion: hourly forecast join, monthly cache, live archive."""

import numpy as np
import pandas as pd

from bikeflow.weather import (
    RAW_FEATURES,
    add_weather_features,
    fetch_live,
    fetch_month,
    load,
    load_live,
)


def synthetic_hours(start, hours, **overrides):
    """Hourly frame with the same columns as the API parser."""
    index = pd.date_range(start, periods=hours, freq="h", tz="UTC")
    values = {
        "temperature_2m": 12.0,
        "apparent_temperature": 11.0,
        "precipitation": 0.0,
        "wind_speed_10m": 8.0,
        "relative_humidity_2m": 60.0,
        "cloud_cover": 20.0,
        "snow_depth": 0.0,
        "visibility": 20000.0,
    }
    values.update(overrides)
    frame = pd.DataFrame({"weather_hour": index})
    for name in RAW_FEATURES:
        frame[name] = np.float32(values[name])
    frame["is_precipitating"] = (frame["precipitation"].fillna(0) > 0).astype("int8")
    frame["is_cold"] = (frame["apparent_temperature"] < 5).astype("int8")
    return frame


def test_add_weather_features_joins_by_the_containing_hour():
    frame = pd.DataFrame(
        {
            "window_start_utc": pd.DatetimeIndex(
                [
                    "2026-10-20 09:00:00+00:00",
                    "2026-10-20 09:30:00+00:00",
                    "2026-10-20 11:00:00+00:00",
                ]
            ),
            "departures": [1, 2, 3],
        }
    )
    weather = synthetic_hours("2026-10-20 09:00", hours=2, precipitation=0.4)
    frame = add_weather_features(frame, weather)
    assert frame.temperature_2m.iloc[0] == 12.0
    # Both halves of the same hour read the same forecast.
    assert frame.temperature_2m.iloc[0] == frame.temperature_2m.iloc[1]
    assert frame.is_precipitating.tolist() == [1, 1, 0]
    # The 11:00 window has no forecast in this two-hour frame.
    assert np.isnan(frame.temperature_2m.iloc[2])


def test_add_weather_features_marks_cold_and_dry_windows():
    frame = pd.DataFrame(
        {
            "window_start_utc": pd.DatetimeIndex(["2026-01-10 08:00:00+00:00"]),
            "departures": [0],
        }
    )
    weather = synthetic_hours("2026-01-10 08:00", hours=1, apparent_temperature=-3.0)
    frame = add_weather_features(frame, weather)
    assert frame.is_cold.iloc[0] == 1
    assert frame.is_precipitating.iloc[0] == 0


def test_load_fetches_each_month_once_then_reads_the_cache(tmp_path):
    calls = []

    def fetcher(start, end):
        calls.append((start, end))
        return synthetic_hours(f"{start} 00:00", hours=24)

    first = load("2026-01-01", "2026-03-01", tmp_path, fetcher=fetcher)
    assert len(calls) == 2  # January and February
    assert len(first) == 48
    assert sorted(p.name for p in tmp_path.glob("*.parquet")) == [
        "forecast_202601.parquet",
        "forecast_202602.parquet",
    ]
    second = load("2026-01-01", "2026-03-01", tmp_path, fetcher=fetcher)
    assert len(calls) == 2  # cache hit, no new requests
    assert len(second) == 48


def test_fetch_month_covers_the_calendar_month(tmp_path):
    seen = {}

    def fetcher(start, end):
        seen["range"] = (start, end)
        return synthetic_hours(f"{start} 00:00", hours=24)

    fetch_month(2026, 2, tmp_path, fetcher=fetcher)
    assert seen["range"] == (pd.Timestamp("2026-02-01").date(), pd.Timestamp("2026-02-28").date())
    fetch_month(2026, 12, tmp_path, fetcher=fetcher)
    assert seen["range"] == (pd.Timestamp("2026-12-01").date(), pd.Timestamp("2026-12-31").date())


def test_live_forecast_is_archived(tmp_path):
    def fetcher():
        return synthetic_hours("2026-11-19 00:00", hours=72)

    assert load_live(tmp_path) is None
    frame = fetch_live(tmp_path, fetcher=fetcher)
    assert len(frame) == 72
    cached = load_live(tmp_path)
    assert cached is not None and len(cached) == 72
