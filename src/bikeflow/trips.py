"""Full monthly CSV scan; diagnostic counts are not a model training dataset."""

import gzip
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from zipfile import ZipFile

import pandas as pd

from .gbfs import utc_now

REQUIRED = {
    "ride_id",
    "rideable_type",
    "started_at",
    "ended_at",
    "start_station_name",
    "start_station_id",
    "end_station_name",
    "end_station_id",
    "start_lat",
    "start_lng",
    "end_lat",
    "end_lng",
    "member_casual",
}


def parse_times(series):
    local = pd.to_datetime(series, format="ISO8601", errors="coerce")
    # Trip timestamps are local NYC time. Ambiguous/nonexistent DST times are
    # explicitly unevaluable rather than silently assigned to the wrong instant.
    return local.dt.tz_localize(
        "America/New_York", ambiguous="NaT", nonexistent="NaT"
    ).dt.tz_convert("UTC")


def distance_m(lat1, lon1, lat2, lon2):
    a, b = math.radians(lat1), math.radians(lat2)
    dlat, dlon = b - a, math.radians(lon2 - lon1)
    h = math.sin(dlat / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.asin(min(1, math.sqrt(h)))


def normalize_name(value):
    return re.sub(r"[^a-z0-9]", "", value.lower())


def map_stations(station_stats, metadata, statuses, tolerance_m=150):
    candidates = defaultdict(list)
    for station in metadata:
        for key in {station["station_id"], station.get("short_name")} - {None, ""}:
            candidates[str(key)].append(station)
    live = {s["station_id"]: s for s in statuses}
    results = []
    for key, stats in sorted(station_stats.items()):
        name = stats["names"].most_common(1)[0][0] if stats["names"] else ""
        coords = stats["coords"]
        lat = stats["lat_sum"] / coords if coords else None
        lon = stats["lon_sum"] / coords if coords else None
        row = {
            "trip_station_id": key,
            "trip_name": name,
            "alternate_names": " | ".join(sorted(stats["names"])),
            "endpoint_count": stats["count"],
            "trip_lat_mean": lat,
            "trip_lon_mean": lon,
            "gbfs_station_id": "",
            "gbfs_name": "",
            "distance_m": None,
            "name_changed": False,
            "active": False,
            "mapping_status": "unmatched",
        }
        matches = candidates.get(key, [])
        if len(matches) > 1:
            row["mapping_status"] = "ambiguous"
        elif len(matches) == 1:
            station = matches[0]
            status = live.get(station["station_id"], {})
            row.update(gbfs_station_id=station["station_id"], gbfs_name=station.get("name", ""))
            row["active"] = all(
                status.get(f) for f in ("is_installed", "is_renting", "is_returning")
            )
            row["name_changed"] = normalize_name(name) != normalize_name(station.get("name", ""))
            if coords and station.get("lat") is not None and station.get("lon") is not None:
                row["distance_m"] = round(distance_m(lat, lon, station["lat"], station["lon"]), 2)
                if row["distance_m"] > tolerance_m:
                    row["mapping_status"] = "coordinate_mismatch"
                elif not row["active"]:
                    row["mapping_status"] = "inactive"
                elif row["name_changed"]:
                    row["mapping_status"] = "renamed_review"
                else:
                    row["mapping_status"] = "matched"
            else:
                row["mapping_status"] = "missing_coordinates"
        results.append(row)
    return results


def audit_archive(archive, snapshot, data_dir, report_dir):
    archive, snapshot = Path(archive), Path(snapshot)
    data_dir, report_dir = Path(data_dir), Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    diagnostic_dir = data_dir / "processed"
    diagnostic_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    with gzip.open(snapshot, "rt") as stream:
        bundle = json.load(stream)
    metadata = bundle["station_information"]["payload"]["data"]["stations"]
    statuses = bundle["station_status"]["payload"]["data"]["stations"]
    stats = defaultdict(
        lambda: {"count": 0, "names": Counter(), "coords": 0, "lat_sum": 0.0, "lon_sum": 0.0}
    )
    windows = {"departures": Counter(), "arrivals": Counter()}
    totals = Counter()
    files = []
    time_min = time_max = None
    with ZipFile(archive) as zipfile:
        members = [
            m
            for m in zipfile.namelist()
            if m.lower().endswith(".csv") and not m.startswith("__MACOSX/")
        ]
        if not members:
            raise ValueError("Archive has no CSV data files")
        for member in members:
            file_rows = 0
            columns = None
            with zipfile.open(member) as stream:
                for chunk in pd.read_csv(stream, dtype="string", chunksize=100_000):
                    columns = list(chunk.columns)
                    missing = REQUIRED - set(columns)
                    if missing:
                        raise ValueError(f"{member} missing columns: {sorted(missing)}")
                    file_rows += len(chunk)
                    totals["rows"] += len(chunk)
                    starts, ends = parse_times(chunk["started_at"]), parse_times(chunk["ended_at"])
                    totals["invalid_or_dst_ambiguous_start_times"] += int(starts.isna().sum())
                    totals["invalid_or_dst_ambiguous_end_times"] += int(ends.isna().sum())
                    totals["negative_duration_rows"] += int((ends < starts).sum())
                    totals["missing_ride_ids"] += int(chunk["ride_id"].isna().sum())
                    cmin, cmax = starts.min(), starts.max()
                    if pd.notna(cmin):
                        time_min = cmin if time_min is None else min(time_min, cmin)
                        time_max = cmax if time_max is None else max(time_max, cmax)
                    for side, times, flow in (
                        ("start", starts, "departures"),
                        ("end", ends, "arrivals"),
                    ):
                        ids = chunk[f"{side}_station_id"].str.strip().replace("", pd.NA)
                        totals[f"missing_{side}_station_ids"] += int(ids.isna().sum())
                        lat = pd.to_numeric(chunk[f"{side}_lat"], errors="coerce")
                        lon = pd.to_numeric(chunk[f"{side}_lng"], errors="coerce")
                        valid_coord = lat.between(-90, 90) & lon.between(-180, 180)
                        totals[f"invalid_{side}_coordinates"] += int((~valid_coord).sum())
                        frame = pd.DataFrame(
                            {
                                "id": ids,
                                "name": chunk[f"{side}_station_name"],
                                "lat": lat.where(valid_coord),
                                "lon": lon.where(valid_coord),
                            }
                        )
                        grouped = frame.groupby("id").agg(
                            count=("id", "size"),
                            coords=("lat", "count"),
                            lat_sum=("lat", "sum"),
                            lon_sum=("lon", "sum"),
                        )
                        for key, values in grouped.iterrows():
                            for field in ("count", "coords", "lat_sum", "lon_sum"):
                                stats[key][field] += values[field]
                        for (key, name), number in frame.groupby(["id", "name"]).size().items():
                            stats[key]["names"][name] += int(number)
                        counts = pd.DataFrame({"id": ids, "window": times.dt.floor("30min")})
                        for key, number in counts.groupby(["id", "window"]).size().items():
                            windows[flow][key] += int(number)
            files.append({"member": member, "rows": file_rows, "columns": columns})
            print(f"Scanned {member}: {file_rows:,} rows", flush=True)
    mapped = map_stations(stats, metadata, statuses)
    mapping = pd.DataFrame(mapped)
    mapping.to_csv(report_dir / "station_mapping.csv", index=False)
    # Twenty busiest verified stations, observed windows only. Missing bins
    # are not asserted to be zero; station operating history is not yet known.
    eligible = mapping[mapping.mapping_status == "matched"]
    selected = set(eligible.nlargest(20, "endpoint_count").trip_station_id)
    table = defaultdict(lambda: {"departures": 0, "arrivals": 0})
    for flow, counter in windows.items():
        for (station, window), number in counter.items():
            if station in selected:
                table[(station, window)][flow] = number
    diagnostic = pd.DataFrame(
        [
            {"station_id": key[0], "window_start_utc": key[1], **value}
            for key, value in sorted(table.items())
        ]
    )
    diagnostic.to_csv(diagnostic_dir / "station_time_diagnostic.csv", index=False)
    result = {
        "generated_at": utc_now(),
        "archive": archive.name,
        "archive_sha256": digest.hexdigest(),
        "csv_parts": files,
        "counts": dict(totals),
        "start_time_min_utc": str(time_min),
        "start_time_max_utc": str(time_max),
        "unique_trip_station_ids": len(stats),
        "snapshot_poll_id": bundle["poll_id"],
        "mapping_counts": dict(Counter(row["mapping_status"] for row in mapped)),
        "mapping_endpoint_counts": {
            category: int(mapping.loc[mapping.mapping_status == category, "endpoint_count"].sum())
            for category in sorted(set(mapping.mapping_status))
        },
        "coordinate_tolerance_m": 150,
        "diagnostic_stations": len(selected),
        "diagnostic_rows": len(diagnostic),
        "limitations": [
            "Monthly trip times interpreted as America/New_York local time.",
            "Ambiguous/nonexistent DST timestamps are excluded from window counts.",
            "No claim of ride-ID deduplication; this is the source archive audit.",
            "Live metadata is current, not historical station operating history.",
            "Names/mean coordinates identify candidates; flagged matches require review.",
            "Diagnostic windows are observed bins, not a complete training grid.",
        ],
    }
    (report_dir / "trip_audit.json").write_text(json.dumps(result, indent=2) + "\n")
    return result
