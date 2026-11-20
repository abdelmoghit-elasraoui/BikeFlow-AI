"""One feature definition for training and serving.

Every feature belongs to a named group. Training builds the groups it is asked
for from the cached sources; serving builds the same groups from the live
weather archive and the station context stored in metadata. The column order is
derived from the group list, so a model artifact lists exactly what it needs and
the API cannot silently feed it a different matrix.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .progress import log
from .weather import WEATHER_FEATURES, add_weather_features

CALENDAR_FEATURES = [
    "station_code",
    "weekday",
    "hour",
    "minute_slot",
    "is_weekend",
    "day_of_year",
]
HOLIDAY_FEATURES = ["is_holiday", "is_day_before_holiday", "is_day_after_holiday"]
STATION_FEATURES = ["latitude", "longitude", "neighbours_500m"]
GROUPS = {
    "calendar": CALENDAR_FEATURES,
    "weather": list(WEATHER_FEATURES),
    "holiday": HOLIDAY_FEATURES,
    "station": STATION_FEATURES,
}
ALL_GROUPS = ("calendar", "weather", "holiday", "station")


def feature_columns(groups):
    """Column order for a group list; unknown groups are a hard error."""
    columns = []
    for group in groups:
        if group not in GROUPS:
            raise ValueError(f"Unknown feature group {group!r}; expected one of {ALL_GROUPS}")
        columns.extend(GROUPS[group])
    return columns


def add_calendar(frame):
    """Calendar features derived from UTC instants; no future information."""
    log(f"Features: calendar columns for {len(frame):,} rows")
    local = pd.DatetimeIndex(frame.window_start_utc).tz_convert("America/New_York")
    frame["weekday"] = local.weekday.astype("int8")
    frame["hour"] = local.hour.astype("int8")
    frame["minute_slot"] = (local.hour * 60 + local.minute).astype("int16")
    frame["is_weekend"] = (local.weekday >= 5).astype("int8")
    frame["day_of_year"] = local.dayofyear.astype("int16")
    return frame


def _nth_weekday(year, month, weekday, n):
    day = date(year, month, 1)
    offset = (weekday - day.weekday()) % 7
    return day + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year, month, weekday):
    next_month = date(year + (month == 12), month % 12 + 1, 1)
    day = next_month - timedelta(days=1)
    offset = (day.weekday() - weekday) % 7
    return day - timedelta(days=offset)


def _observed(day):
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def us_holidays(year):
    """Federal holidays as observed in New York.

    A holiday Monday behaves like a Sunday, which is the one day-of-week signal
    the calendar cannot see. School breaks and local observances are not
    included; they need a source we do not have.
    """
    days = [
        _observed(date(year, 1, 1)),  # New Year
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King
        _nth_weekday(year, 2, 0, 3),  # Presidents
        _last_weekday(year, 5, 0),  # Memorial
        _observed(date(year, 6, 19)),  # Juneteenth
        _observed(date(year, 7, 4)),  # Independence
        _nth_weekday(year, 9, 0, 1),  # Labor
        _nth_weekday(year, 10, 0, 2),  # Columbus
        _observed(date(year, 11, 11)),  # Veterans
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),  # Christmas
    ]
    return sorted(set(days))


def holiday_index(years):
    days = sorted({day for year in years for day in us_holidays(year)})
    return pd.DatetimeIndex(pd.to_datetime(days)).tz_localize("America/New_York")


def add_holidays(frame, years=None):
    """Holiday flags by New York local date."""
    local = pd.DatetimeIndex(frame.window_start_utc).tz_convert("America/New_York")
    days = local.normalize()
    if years is None:
        years = range(int(days.min().year), int(days.max().year) + 2)
    holidays = holiday_index(years)
    before = holidays - pd.Timedelta(days=1)
    after = holidays + pd.Timedelta(days=1)
    frame["is_holiday"] = (holidays.get_indexer(days) >= 0).astype("int8")
    frame["is_day_before_holiday"] = (before.get_indexer(days) >= 0).astype("int8")
    frame["is_day_after_holiday"] = (after.get_indexer(days) >= 0).astype("int8")
    log(
        f"Features: holidays {int(frame.is_holiday.sum()):,} windows, "
        f"{len(holidays)} dates from {holidays.min().date()} to {holidays.max().date()}"
    )
    return frame


def station_context(mapping_path, min_distance_m=500.0):
    """Coordinates and nearby-station count for every matched trip station.

    A station's level is already in the per-key offset; coordinates matter
    through interactions (rain hurts a park station more than a subway one) and
    through cold start. Neighbour count is a cheap density proxy: stations in
    Midtown are 200m apart, outer-borough ones are not.
    """
    mapping = pd.read_csv(Path(mapping_path), dtype="string")
    matched = mapping[mapping.mapping_status == "matched"].copy()
    matched = matched.dropna(subset=["trip_lat_mean", "trip_lon_mean"])
    matched["trip_lat_mean"] = matched.trip_lat_mean.astype(float)
    matched["trip_lon_mean"] = matched.trip_lon_mean.astype(float)
    latitude = np.radians(matched.trip_lat_mean.to_numpy())
    longitude = np.radians(matched.trip_lon_mean.to_numpy())
    # Haversine, pairwise: 2,130 stations is ~4.5M distances, a few hundred MB
    # at float32 and fast enough to do once per training run.
    dlat = latitude[:, None] - latitude[None, :]
    dlon = longitude[:, None] - longitude[None, :]
    a = (
        np.sin(dlat / 2) ** 2
        + np.cos(latitude)[:, None] * np.cos(latitude)[None, :] * np.sin(dlon / 2) ** 2
    )
    distance = 2 * 6_371_000 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
    neighbours = (distance <= min_distance_m).sum(axis=1) - 1
    context = pd.DataFrame(
        {
            "station_id": matched.trip_station_id.astype(str).to_numpy(),
            "latitude": matched.trip_lat_mean.to_numpy(np.float32),
            "longitude": matched.trip_lon_mean.to_numpy(np.float32),
            "neighbours_500m": neighbours.astype("int16"),
        }
    )
    log(f"Features: station context for {len(context):,} stations")
    return context


def add_station_context(frame, context):
    """Assign coordinates and density by station code, in place."""
    context = context.sort_values("station_code")
    codes = context.station_code.to_numpy(np.int64)
    wanted = frame.station_code.to_numpy(np.int64)
    positions = np.searchsorted(codes, wanted)
    valid = positions < len(codes)
    valid[valid] = codes[positions[valid]] == wanted[valid]
    for name in STATION_FEATURES:
        values = np.full(len(frame), np.nan, dtype=np.float32)
        source = context[name].to_numpy(np.float32)
        values[valid] = source[positions[valid]]
        frame[name] = values
    if "neighbours_500m" in frame:
        frame["neighbours_500m"] = frame["neighbours_500m"].fillna(0).astype("int16")
    log(f"Features: station context joined, {int((~valid).sum()):,} rows without a match")
    return frame


def build(frame, groups, weather=None, context=None):
    """Apply every requested group, then check the matrix is complete."""
    groups = tuple(groups)
    if "calendar" in groups:
        add_calendar(frame)
    if "weather" in groups:
        if weather is None:
            raise ValueError("The weather group needs a weather frame")
        add_weather_features(frame, weather)
    if "holiday" in groups:
        add_holidays(frame)
    if "station" in groups:
        if context is None:
            raise ValueError("The station group needs station context")
        add_station_context(frame, context)
    missing = [name for name in feature_columns(groups) if name not in frame.columns]
    if missing:
        raise ValueError(f"Feature groups produced no columns for {missing}")
    return frame
