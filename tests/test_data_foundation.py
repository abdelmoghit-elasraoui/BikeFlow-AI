import gzip
import json

import pandas as pd
import pytest

from bikeflow import collector
from bikeflow.gbfs import discover, utc_now, validate_status
from bikeflow.trips import map_stations, parse_times


def test_discovery_supports_localized_and_v3_shapes():
    feeds = [
        {"name": n, "url": f"https://example.com/{n}"}
        for n in ("station_information", "station_status")
    ]
    assert discover({"payload": {"data": {"en": {"feeds": feeds}}}}) == discover(
        {"payload": {"data": {"feeds": feeds}}}
    )
    with pytest.raises(ValueError):
        discover({"payload": {"data": {"feeds": []}}})


def test_invalid_counts_and_missing_timestamps_are_flagged():
    row = {
        "num_bikes_available": -1,
        "num_docks_available": True,
        "is_installed": 1,
        "is_renting": 1,
        "is_returning": 1,
    }
    assert set(validate_status(row)) == {
        "invalid_num_bikes_available",
        "invalid_num_docks_available",
        "invalid_last_reported",
    }


def test_dst_is_explicit_and_windows_use_utc():
    parsed = parse_times(
        pd.Series(["2026-02-01 00:10:00", "2026-03-08 02:30:00", "2026-11-01 01:30:00"])
    )
    assert str(parsed.iloc[0]) == "2026-02-01 05:10:00+00:00"
    assert parsed.iloc[1:].isna().all()


def test_mapping_rejects_ambiguous_and_distant_matches():
    stats = {"x": {"names": {"Old": 1}, "count": 1, "coords": 1, "lat_sum": 40.7, "lon_sum": -74.0}}
    # map_stations needs a Counter, not just a dict.
    from collections import Counter

    stats["x"]["names"] = Counter(stats["x"]["names"])
    first = {"station_id": "a", "short_name": "x", "name": "Old", "lat": 41, "lon": -74}
    assert map_stations(stats, [first], [])[0]["mapping_status"] == "coordinate_mismatch"
    second = {**first, "station_id": "b"}
    assert map_stations(stats, [first, second], [])[0]["mapping_status"] == "ambiguous"


def test_raw_payloads_and_repeated_source_survive_collection(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    now = int(datetime.now(timezone.utc).timestamp())
    row = {
        "station_id": "s",
        "num_bikes_available": 2,
        "num_docks_available": 3,
        "is_installed": 1,
        "is_renting": 1,
        "is_returning": 1,
        "last_reported": now,
    }

    def fetch(url):
        if url == "https://example.com/discovery":
            data = {
                "feeds": [
                    {"name": n, "url": "https://example.com/" + n}
                    for n in ("station_information", "station_status")
                ]
            }
        else:
            data = {"stations": [row]}
        return {
            "url": url,
            "retrieved_at": utc_now(),
            "payload": {"data": data, "ttl": 60, "last_updated": now},
        }

    monkeypatch.setattr(collector, "get_json", fetch)
    one = collector.collect_once(tmp_path, "https://example.com/discovery")
    two = collector.collect_once(tmp_path, "https://example.com/discovery")
    assert one["eligible"] == 1 and not one["repeated_source"]
    assert two["repeated_source"]
    with gzip.open(two["raw_path"], "rt") as stream:
        assert json.load(stream)["station_status"]["payload"]["data"]["stations"] == [row]
    assert collector.coverage(tmp_path)["polls"] == 2


def test_failed_poll_records_outage_without_fake_observations(tmp_path, monkeypatch):
    def fail(*args):
        raise ValueError("malformed payload")

    monkeypatch.setattr(collector, "get_json", fail)
    assert collector.collect(tmp_path, "https://example.com", count=1) == 1
    report = collector.coverage(tmp_path)
    assert report["polls"] == 0 and len(report["errors"]) == 1


def test_all_csv_parts_are_scanned_and_arrivals_use_end_time(tmp_path):
    from zipfile import ZipFile

    from bikeflow.trips import audit_archive

    archive = tmp_path / "month.zip"
    columns = (
        "ride_id,rideable_type,started_at,ended_at,start_station_name,start_station_id,"
        "end_station_name,end_station_id,start_lat,start_lng,end_lat,end_lng,member_casual\n"
    )
    trip = (
        "r,classic_bike,2026-02-01 00:10:00,2026-02-01 00:40:00,Station,x,Station,x,"
        "40.7,-74,40.7,-74,member\n"
    )
    missing = trip.replace("Station,x,Station,x", "Station,,Station,x")
    with ZipFile(archive, "w") as z:
        z.writestr("part1.csv", columns + trip)
        z.writestr("part2.csv", columns + missing)
        z.writestr("__MACOSX/._part1.csv", "ignored metadata")
    station = {
        "station_id": "uuid",
        "short_name": "x",
        "name": "Station",
        "lat": 40.7,
        "lon": -74,
        "is_installed": 1,
        "is_renting": 1,
        "is_returning": 1,
    }
    snapshot = tmp_path / "snapshot.json.gz"
    with gzip.open(snapshot, "wt") as f:
        json.dump(
            {
                "poll_id": "test",
                **{
                    name: {"payload": {"data": {"stations": [station]}}}
                    for name in ("station_information", "station_status")
                },
            },
            f,
        )
    result = audit_archive(archive, snapshot, tmp_path / "data", tmp_path / "reports")
    assert len(result["csv_parts"]) == 2 and result["counts"]["rows"] == 2
    assert result["counts"]["missing_start_station_ids"] == 1
    assert result["mapping_counts"] == {"matched": 1}
    diagnostic = pd.read_csv(tmp_path / "data/processed/station_time_diagnostic.csv")
    assert list(diagnostic.departures) == [1, 0]
    assert list(diagnostic.arrivals) == [0, 2]
