"""Hourly weather from archived forecast runs, joined to forecast windows.

Training must see what a forecaster saw at issue time, not the observation that
came later, so the source is Open-Meteo's historical forecast API rather than a
reanalysis product. Serving archives the live forecast on the same variables, so
train and serve share one definition. Cached one month per file; no API key.

Weather is hourly, so the 30- and 60-minute windows read the value for the hour
that contains the window start. That is deliberate: rain at 09:05 and rain at
09:35 are the same information at these horizons.
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

from .model import log

FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
LIVE_URL = "https://api.open-meteo.com/v1/forecast"
NEW_YORK = {"latitude": 40.7306, "longitude": -73.9352}
# Kept small on purpose: each column costs 160MB over 40M rows. These are the
# variables that move bike demand: temperature, rain, wind, sun, visibility.
RAW_FEATURES = [
    "temperature_2m",
    "apparent_temperature",
    "precipitation",
    "wind_speed_10m",
    "relative_humidity_2m",
    "cloud_cover",
    "snow_depth",
    "visibility",
]
DERIVED_FEATURES = ["is_precipitating", "is_cold"]
WEATHER_FEATURES = [*RAW_FEATURES, *DERIVED_FEATURES]
COLD_CELSIUS = 5.0


def _request_json(url, attempts=3, timeout=40):
    for attempt in range(attempts):
        try:
            request = Request(url, headers={"User-Agent": "BikeFlowAI/0.1 academic forecasting"})
            with urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
            if "hourly" not in payload:
                raise ValueError(f"Weather response has no hourly block: {payload}")
            return payload
        except (HTTPError, URLError, TimeoutError, ValueError, OSError):
            if attempt + 1 == attempts:
                raise
            time.sleep(2**attempt)


def _frame_from_payload(payload):
    hourly = payload["hourly"]
    frame = pd.DataFrame(hourly)
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    frame = frame.rename(columns={"time": "weather_hour"})
    for name in RAW_FEATURES:
        if name not in frame:
            frame[name] = np.nan
        frame[name] = pd.to_numeric(frame[name], errors="coerce").astype("float32")
    frame["is_precipitating"] = (frame["precipitation"].fillna(0) > 0).astype("int8")
    frame["is_cold"] = (frame["apparent_temperature"] < COLD_CELSIUS).astype("int8")
    return (
        frame[["weather_hour", *RAW_FEATURES, *DERIVED_FEATURES]]
        .sort_values("weather_hour")
        .reset_index(drop=True)
    )


def fetch_month(year, month, cache_dir, fetcher=None):
    """One calendar month of hourly forecast history, cached as Parquet."""
    cache_dir = Path(cache_dir)
    path = cache_dir / f"forecast_{year:04d}{month:02d}.parquet"
    if path.exists():
        return pd.read_parquet(path)
    start = date(year, month, 1)
    next_month = date(year + (month == 12), month % 12 + 1, 1)
    end = (pd.Timestamp(next_month) - pd.Timedelta(days=1)).date()
    if fetcher is None:
        query = urlencode(
            {
                **NEW_YORK,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "hourly": ",".join(RAW_FEATURES),
                "timezone": "UTC",
            }
        )
        frame = _frame_from_payload(_request_json(f"{FORECAST_URL}?{query}"))
    else:
        frame = fetcher(start, end)
    cache_dir.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)
    return frame


def load(start, end, cache_dir, fetcher=None):
    """Hourly weather covering [start, end), assembled from monthly caches."""
    first = pd.Timestamp(start).normalize().replace(day=1)
    last = pd.Timestamp(end)
    months = []
    cursor = first
    while cursor < last:
        months.append((cursor.year, cursor.month))
        cursor += pd.offsets.MonthBegin(1)
    frames = []
    for year, month in months:
        log(f"Weather: month {year}-{month:02d}")
        frames.append(fetch_month(year, month, cache_dir, fetcher))
    frame = pd.concat(frames, ignore_index=True)
    frame = frame.drop_duplicates("weather_hour").sort_values("weather_hour")
    log(
        f"Weather: {len(frame):,} hours from {frame.weather_hour.min()} "
        f"to {frame.weather_hour.max()}"
    )
    return frame.reset_index(drop=True)


def add_weather_features(frame, weather):
    """Join weather in place by the hour containing each window start.

    Column assignment rather than a merge: at 40M rows a merge copies the whole
    frame, while eight float32 columns cost 1.3GB and are dropped with it.
    """
    hours = pd.DatetimeIndex(frame.window_start_utc).floor("h")
    index = pd.DatetimeIndex(weather["weather_hour"])
    positions = index.get_indexer(hours)
    missing = positions < 0
    safe = np.maximum(positions, 0)
    for name in RAW_FEATURES:
        values = weather[name].to_numpy(np.float32)[safe].copy()
        if missing.any():
            values[missing] = np.nan
        frame[name] = values
    frame["is_precipitating"] = (frame["precipitation"].fillna(0) > 0).astype("int8")
    frame["is_cold"] = (frame["apparent_temperature"] < COLD_CELSIUS).astype("int8")
    log(f"Weather: joined {len(frame):,} windows, {int(missing.sum()):,} without a forecast")
    return frame


def fetch_live(cache_dir, fetcher=None):
    """Archive the current hourly forecast, for serving.

    Returns rows from two days back to three days ahead, so a restarted service
    still finds the hour it needs.
    """
    cache_dir = Path(cache_dir)
    if fetcher is None:
        query = urlencode(
            {
                **NEW_YORK,
                "hourly": ",".join(RAW_FEATURES),
                "timezone": "UTC",
                "past_days": 2,
                "forecast_days": 3,
            }
        )
        frame = _frame_from_payload(_request_json(f"{LIVE_URL}?{query}"))
    else:
        frame = fetcher()
    path = cache_dir / "live_forecast.parquet"
    cache_dir.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)
    log(
        f"Weather live: {len(frame):,} hours, "
        f"{frame.weather_hour.min()} to {frame.weather_hour.max()}"
    )
    return frame


def load_live(cache_dir):
    """The archived live forecast, or None when the collector has not run."""
    path = Path(cache_dir) / "live_forecast.parquet"
    if not path.exists():
        return None
    frame = pd.read_parquet(path)
    if frame.empty:
        return None
    return frame
