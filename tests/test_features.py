"""The feature layer: groups, holidays, station context, one build path."""

import numpy as np
import pandas as pd

from bikeflow.features import (
    add_holidays,
    add_station_context,
    build,
    feature_columns,
    holiday_index,
    station_context,
    us_holidays,
)


def frame_at(*stamps):
    return pd.DataFrame(
        {
            "station_code": pd.Series([0] * len(stamps), dtype="int32"),
            "window_start_utc": pd.DatetimeIndex(list(stamps), tz="UTC"),
            "departures": 0,
        }
    )


def test_feature_columns_follow_the_group_order():
    assert feature_columns(["calendar"])[:3] == ["station_code", "weekday", "hour"]
    assert "temperature_2m" in feature_columns(["calendar", "weather"])
    assert feature_columns(["calendar", "holiday"])[-3:] == [
        "is_holiday",
        "is_day_before_holiday",
        "is_day_after_holiday",
    ]
    try:
        feature_columns(["nope"])
    except ValueError as error:
        assert "Unknown feature group" in str(error)
    else:
        raise AssertionError("unknown group must fail")


def test_us_holidays_include_moving_and_observed_days():
    days = us_holidays(2026)
    assert pd.Timestamp("2026-11-26").date() in days  # Thanksgiving, 4th Thursday
    assert pd.Timestamp("2026-07-03").date() in days  # 4 July 2026 is a Saturday
    assert pd.Timestamp("2026-09-07").date() in days  # Labor Day
    assert holiday_index([2026]).tz is not None


def test_add_holidays_flags_the_day_before_and_after():
    frame = frame_at(
        "2026-11-25 15:00:00+00:00",  # day before Thanksgiving
        "2026-11-26 15:00:00+00:00",  # Thanksgiving
        "2026-11-27 15:00:00+00:00",  # day after
        "2026-11-24 15:00:00+00:00",  # ordinary Tuesday
    )
    frame = add_holidays(frame)
    assert frame.is_holiday.tolist() == [0, 1, 0, 0]
    assert frame.is_day_before_holiday.tolist() == [1, 0, 0, 0]
    assert frame.is_day_after_holiday.tolist() == [0, 0, 1, 0]


def test_station_context_counts_neighbours_within_500m(tmp_path):
    mapping = tmp_path / "mapping.csv"
    pd.DataFrame(
        {
            "trip_station_id": ["1", "2", "3"],
            "trip_name": ["a", "b", "c"],
            "trip_lat_mean": [40.0000, 40.0020, 40.0500],
            "trip_lon_mean": [-73.0000, -73.0000, -73.0000],
            "mapping_status": ["matched", "matched", "matched"],
        }
    ).to_csv(mapping, index=False)
    context = station_context(mapping)
    by_id = context.set_index("station_id")
    # 0.002 degrees of latitude is ~222m, so 1 and 2 are neighbours; 3 is not.
    assert by_id.loc["1", "neighbours_500m"] == 1
    assert by_id.loc["2", "neighbours_500m"] == 1
    assert by_id.loc["3", "neighbours_500m"] == 0
    assert np.isclose(by_id.loc["1", "latitude"], 40.0)


def test_add_station_context_maps_codes_and_fills_unknown(tmp_path):
    mapping = tmp_path / "mapping.csv"
    pd.DataFrame(
        {
            "trip_station_id": ["1"],
            "trip_name": ["a"],
            "trip_lat_mean": [40.0],
            "trip_lon_mean": [-73.0],
            "mapping_status": ["matched"],
        }
    ).to_csv(mapping, index=False)
    context = station_context(mapping)
    context["station_code"] = [0]
    frame = pd.DataFrame(
        {
            "station_code": pd.Series([0, 2], dtype="int32"),
            "window_start_utc": pd.DatetimeIndex(
                ["2026-10-20 09:00:00+00:00", "2026-10-20 09:00:00+00:00"]
            ),
        }
    )
    frame = add_station_context(frame, context)
    assert frame.latitude.iloc[0] == 40.0
    assert np.isnan(frame.latitude.iloc[1])  # unseen code
    assert frame.neighbours_500m.iloc[1] == 0


def test_build_requires_the_sources_its_groups_need():
    frame = frame_at("2026-10-20 09:00:00+00:00")
    try:
        build(frame, ["calendar", "weather"])
    except ValueError as error:
        assert "weather frame" in str(error)
    else:
        raise AssertionError("weather group without a frame must fail")
    calendar = build(frame_at("2026-10-20 09:00:00+00:00"), ["calendar"])
    assert calendar.hour.iloc[0] == 5  # 09:00 UTC is 05:00 in New York
