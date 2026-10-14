"""Session 2 checks: DST, window nesting, eligibility, no-leakage baseline."""

import pandas as pd

from bikeflow.session2 import (
    evaluate,
    floor_windows,
    nyc_keys,
    train_key_means,
    write_month_partition,
)


def test_dst_spring_gap_and_fall_overlap_are_excluded():
    from bikeflow.trips import parse_times

    parsed = parse_times(
        pd.Series(
            [
                "2025-03-09 01:30:00",  # valid EST morning
                "2025-03-09 02:30:00",  # nonexistent (spring forward)
                "2025-11-02 01:30:00",  # ambiguous (fall back)
                "2025-11-02 03:30:00",  # valid EST morning
            ]
        )
    )
    assert str(parsed.iloc[0]) == "2025-03-09 06:30:00+00:00"
    assert pd.isna(parsed.iloc[1]) and pd.isna(parsed.iloc[2])
    assert str(parsed.iloc[3]) == "2025-11-02 08:30:00+00:00"


def test_60min_window_equals_two_30min_halves():
    times = pd.DatetimeIndex(["2026-01-05 05:10:00+00:00", "2026-01-05 05:40:00+00:00"], tz="UTC")
    halves = floor_windows(pd.Series(times), 30).tolist()
    full = floor_windows(pd.Series(times), 60).tolist()
    assert halves[0] != halves[1]
    assert full[0] == full[1] == pd.Timestamp("2026-01-05 05:00:00+00:00", tz="UTC")


def test_nyc_keys_use_local_weekday_and_slot():
    # 2026-01-05 05:10 UTC is a Monday 00:10 in New York.
    keys = nyc_keys(pd.DatetimeIndex(["2026-01-05 05:10:00+00:00"], tz="UTC"))
    assert keys.weekday.iloc[0] == 0
    assert keys.slot_30.iloc[0] == 0
    assert keys.slot_60.iloc[0] == 0


def _toy_events():
    # Station A trips on two Mondays; station B trips once. Span covers Jan only.
    rows = [
        ("A", "2026-01-05 05:10:00+00:00"),  # Monday slot_30 0
        ("A", "2026-01-05 05:12:00+00:00"),
        ("A", "2026-01-12 05:10:00+00:00"),  # next Monday, same key
        ("B", "2026-01-06 05:10:00+00:00"),  # Tuesday
    ]
    frame = pd.DataFrame(rows, columns=["station", "window"])
    frame["window"] = pd.DatetimeIndex(frame.window, tz="UTC")
    frame["n"] = 1
    grouped = frame.groupby(["station", "window"], observed=True).n.sum().reset_index()
    spans = {"A": ("2026-01-01", "2026-01-31"), "B": ("2026-01-01", "2026-01-31")}
    return grouped, spans


def test_baseline_uses_train_only_and_zero_fills_genuine_zeros():
    events, spans = _toy_events()
    test_start = "2026-01-18"  # last 14 days of January
    means = train_key_means(events, spans, test_start, 30)
    monday_key = ("A", 0, 0)
    # Train has 3 trips over 2 eligible Monday-slot windows -> mean 1.5.
    assert means[monday_key] == 1.5
    before = dict(means)
    mutated = events.copy()
    mutated.loc[mutated.window >= test_start, "n"] = 999
    assert train_key_means(mutated, spans, test_start, 30) == before


def test_outside_span_is_unevaluable_not_zero():
    events, spans = _toy_events()
    spans = {"A": ("2026-01-05", "2026-01-12")}  # narrow span
    result = evaluate(events, spans, {}, 0.0, "2026-01-01", 30)
    # 8 days x 48 windows for A only; B has no span so it contributes nothing.
    assert result["test_windows"] == 8 * 48
    assert result["fallback_rate"] == 1.0


def test_parquet_counts_match_event_totals(tmp_path):
    from zipfile import ZipFile

    from bikeflow.session2 import scan_month

    cols = (
        "ride_id,rideable_type,started_at,ended_at,start_station_name,start_station_id,"
        "end_station_name,end_station_id,start_lat,start_lng,end_lat,end_lng,member_casual"
    )
    trip = "r1,classic_bike,2026-01-05 00:10:00,2026-01-05 00:40:00,A,A,B,B,40.7,-74,40.71,-74.01,member\n"
    archive = tmp_path / "m.zip"
    with ZipFile(archive, "w") as z:
        z.writestr("t.csv", cols + "\n" + trip)
    evidence, events, spans = scan_month(archive, {"A", "B"})
    assert evidence["counts"]["rows"] == 1
    for frame in events.values():
        if not frame.empty:
            assert str(frame.window.dtype) == "datetime64[ns, UTC]"
            assert str(frame.station.dtype) == "string"
    out = tmp_path / "p"
    assert (
        write_month_partition(events[(30, "dep")], events[(30, "arr")], spans, 2026, 1, 30, out)
        == 1
    )
    got = pd.read_parquet(out)
    assert int(got.departures.sum()) == 1
    assert int(got.arrivals.sum()) == 1
    assert (got.eligible == 1).all()
