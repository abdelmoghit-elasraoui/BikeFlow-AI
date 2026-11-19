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
