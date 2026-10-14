"""Session 2: multi-month station-time targets and baseline v0.

Local-only; every output goes under data/ (gitignored), never to GitHub.

Pipeline: scan monthly trip ZIPs -> aggregate matched-station arrivals and
departures into 30-minute and 60-minute UTC windows -> write partitioned
Parquet (year=/month=/) -> historical-average baseline with MAE on a held-out
final 14 days.

Eligibility rule (documented heuristic v0): the trip archive does not record
station opening/closing dates, so a station is assumed operating between its
first and last observed trip day (inclusive). Windows inside that span are
eligible and zero-filled when empty (genuine zeros). Windows outside the span
are not emitted at all (unevaluable, never silent zeros). Unmatched trip IDs
are excluded entirely. Real operating history refines this in later sessions.
"""

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pandas as pd

from .gbfs import utc_now
from .trips import REQUIRED, parse_times

HORIZONS = {30: "30min", 60: "60min"}
TEST_DAYS = 14
CHUNK = 100_000
STATION_CHUNK = 250
PROGRESS_EVERY = 500

_STARTED = time.monotonic()


def log(message):
    """Timestamped progress line: wall-clock UTC plus elapsed seconds."""
    elapsed = time.monotonic() - _STARTED
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{now} UTC +{elapsed:7.1f}s] {message}", flush=True)


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def floor_windows(times, horizon_min):
    """Floor UTC datetimes to explicit 30/60-minute window starts."""
    return times.dt.floor(HORIZONS[horizon_min])


def nyc_keys(utc):
    """Derive NYC weekday (Mon=0) and slot indexes for both horizons."""
    idx = pd.DatetimeIndex(utc)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    local = idx.tz_convert("America/New_York")
    return pd.DataFrame(
        {
            "weekday": local.weekday.astype("int16"),
            "slot_30": (local.hour * 2 + local.minute // 30).astype("int16"),
            "slot_60": local.hour.astype("int16"),
        }
    )


def matched_ids(mapping_path):
    """Trip IDs with a verified active live match. Everything else is out."""
    mapping = pd.read_csv(mapping_path, dtype="string")
    return set(mapping.loc[mapping.mapping_status == "matched", "trip_station_id"])


def scan_month(archive, matched):
    """Chunk-scan one monthly ZIP into per-window event counts.

    Returns (evidence, events, spans). events[(horizon, flow)] is a DataFrame
    of (station, window, n) summed over the month; spans maps station to its
    (first_day, last_day) ISO date strings. Only matched stations are kept.
    """
    archive = Path(archive)
    evidence = {"archive": archive.name, "sha256": sha256_of(archive), "parts": []}
    counts = {"rows": 0, "bad_times": 0, "missing_ids": 0, "unmatched_kept_out": 0}
    time_min = time_max = None
    parts = {(h, f): [] for h in (30, 60) for f in ("dep", "arr")}
    span_frames = []
    with ZipFile(archive) as zipfile:
        members = [
            m
            for m in zipfile.namelist()
            if m.lower().endswith(".csv") and not m.startswith("__MACOSX/")
        ]
        if not members:
            raise ValueError(f"{archive.name} has no CSV data files")
        for member in members:
            part_rows = 0
            with zipfile.open(member) as stream:
                for n_chunk, chunk in enumerate(
                    pd.read_csv(stream, dtype="string", chunksize=CHUNK)
                ):
                    if n_chunk % 10 == 0:
                        log(
                            f"Scan {archive.name}/{member}: chunk {n_chunk} ({part_rows:,} rows so far)"
                        )
                    missing = REQUIRED - set(chunk.columns)
                    if missing:
                        raise ValueError(f"{member} missing columns: {sorted(missing)}")
                    part_rows += len(chunk)
                    counts["rows"] += len(chunk)
                    starts, ends = parse_times(chunk["started_at"]), parse_times(chunk["ended_at"])
                    counts["bad_times"] += int(starts.isna().sum() + ends.isna().sum())
                    for side, times, flow in (("start", starts, "dep"), ("end", ends, "arr")):
                        ids = chunk[f"{side}_station_id"].str.strip().replace("", pd.NA)
                        counts["missing_ids"] += int(ids.isna().sum())
                        ok = pd.DataFrame({"id": ids, "t": times}).dropna()
                        known = ok.id.isin(matched)
                        counts["unmatched_kept_out"] += int((~known).sum())
                        ok = ok[known]
                        if ok.empty:
                            continue
                        cmin, cmax = ok.t.min(), ok.t.max()
                        time_min = cmin if time_min is None else min(time_min, cmin)
                        time_max = cmax if time_max is None else max(time_max, cmax)
                        span_frames.append(
                            ok.assign(day=ok.t.dt.date.astype("string"))
                            .groupby("id", observed=True)
                            .day.agg(["min", "max"])
                        )
                        for horizon in (30, 60):
                            grouped = (
                                pd.DataFrame(
                                    {"id": ok.id.values, "w": floor_windows(ok.t, horizon).values}
                                )
                                .groupby(["id", "w"], observed=True)
                                .size()
                            )
                            parts[(horizon, flow)].append(grouped)
            evidence["parts"].append({"member": member, "rows": part_rows})
            log(f"Scanned {member}: {part_rows:,} rows")
    events = {}
    for key, series_list in parts.items():
        if not series_list:
            events[key] = pd.DataFrame(
                {
                    "station": pd.Series(dtype="string"),
                    "window": pd.Series(dtype="datetime64[ns, UTC]"),
                    "n": pd.Series(dtype="int32"),
                }
            )
            continue
        total = pd.concat(series_list).groupby(level=[0, 1]).sum()
        frame = total.reset_index()
        frame.columns = ["station", "window", "n"]
        frame["station"] = frame.station.astype("string")
        # .values above drops tz; these wall times are UTC, so re-localize.
        frame["window"] = pd.DatetimeIndex(frame.window, tz="UTC")
        frame["n"] = frame.n.astype("int32")
        events[key] = frame
    spans = {}
    if span_frames:
        span_all = pd.concat(span_frames).groupby(level=0).agg({"min": "min", "max": "max"})
        spans = {station: (row["min"], row["max"]) for station, row in span_all.iterrows()}
    evidence["counts"] = counts
    evidence["start_min_utc"] = str(time_min)
    evidence["start_max_utc"] = str(time_max)
    evidence["matched_stations_seen"] = len(spans)
    return evidence, events, spans


def merge_spans(all_spans):
    """Union per-month spans into global first/last observed days."""
    merged = {}
    for spans in all_spans:
        for station, (first, last) in spans.items():
            if station in merged:
                merged[station] = (min(merged[station][0], first), max(merged[station][1], last))
            else:
                merged[station] = (first, last)
    return merged


def month_windows(year, month, horizon):
    """Every window start in a calendar month (UTC, explicit boundaries)."""
    start = pd.Timestamp(year=year, month=month, day=1, tz="UTC")
    end = start + pd.offsets.MonthBegin(1)
    return pd.date_range(start, end, freq=HORIZONS[horizon], inclusive="left")


def write_month_partition(events_dep, events_arr, spans, year, month, horizon, out_dir):
    """Zero-fill eligible windows for one month; outside-span stays absent."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    windows = month_windows(year, month, horizon)
    month_dep = events_dep[events_dep.window.dt.strftime("%Y-%m") == f"{year}-{month:02d}"]
    month_arr = events_arr[events_arr.window.dt.strftime("%Y-%m") == f"{year}-{month:02d}"]
    stations = sorted(spans)
    files = 0
    n_groups = (len(stations) + STATION_CHUNK - 1) // STATION_CHUNK
    for g, i in enumerate(range(0, len(stations), STATION_CHUNK)):
        log(f"Grid {horizon}min {year}-{month:02d}: station chunk {g + 1}/{n_groups}")
        group = stations[i : i + STATION_CHUNK]
        frame = pd.DataFrame(
            {
                "station_id": np.repeat(np.array(group), len(windows)),
                "window_start_utc": np.tile(windows.values, len(group)),
            }
        )
        frame["window_start_utc"] = pd.to_datetime(frame.window_start_utc, utc=True)
        span_frame = pd.DataFrame(
            {
                "station_id": group,
                "first": [spans[s][0] for s in group],
                "last": [spans[s][1] for s in group],
            }
        )
        frame = frame.merge(span_frame, on="station_id")
        frame["station_id"] = frame.station_id.astype("string")
        days = frame.window_start_utc.dt.date.astype("string")
        frame = frame[(days >= frame["first"]) & (days <= frame["last"])]
        if frame.empty:
            continue
        dep_lookup = month_dep.set_index(["station", "window"]).n if not month_dep.empty else None
        arr_lookup = month_arr.set_index(["station", "window"]).n if not month_arr.empty else None
        keys = pd.MultiIndex.from_arrays([frame.station_id, frame.window_start_utc])
        frame["departures"] = (
            keys.map(dep_lookup).fillna(0).astype("int32")
            if dep_lookup is not None
            else np.int32(0)
        )
        frame["arrivals"] = (
            keys.map(arr_lookup).fillna(0).astype("int32")
            if arr_lookup is not None
            else np.int32(0)
        )
        frame["eligible"] = np.int32(1)
        out = frame[["station_id", "window_start_utc", "departures", "arrivals", "eligible"]]
        out.to_parquet(out_dir / f"part-{files:03d}.parquet", index=False)
        files += 1
    return files


def split_day(global_max_day):
    """First date of the held-out final 14-day test period."""
    return (pd.Timestamp(global_max_day) - pd.Timedelta(days=TEST_DAYS - 1)).date().isoformat()


def _span_days(spans, start_bound=None, end_bound=None):
    """One (station, day) row per eligible calendar day (vectorized)."""
    frames = []
    for station, (first, last) in spans.items():
        start = first if start_bound is None else max(first, start_bound)
        end = last if end_bound is None else min(last, end_bound)
        if start > end:
            continue
        frames.append(
            pd.DataFrame({"station": station, "day": pd.date_range(start, end, freq="D")})
        )
    if not frames:
        return pd.DataFrame(
            {"station": pd.Series(dtype="string"), "day": pd.Series(dtype="datetime64[ns]")}
        )
    out = pd.concat(frames, ignore_index=True)
    out["station"] = out.station.astype("string")
    return out


def train_key_means(events, spans, test_start, horizon):
    """Mean trips per (station, weekday, slot) over eligible train windows.

    Empty-but-eligible windows count as genuine zeros via span arithmetic:
    every slot occurs exactly once per day, so eligible windows per key =
    number of train days in the station span with that weekday.
    """
    slot = f"slot_{horizon}"
    n_slots = 24 * 60 // horizon
    log(f"Baseline-train {horizon}min: aggregating event totals")
    totals = pd.DataFrame(
        {
            "station": pd.Series(dtype="string"),
            "weekday": pd.Series(dtype="int16"),
            slot: pd.Series(dtype="int16"),
            "total": pd.Series(dtype="int64"),
        }
    )
    if not events.empty:
        train = events.copy()
        train["day"] = train.window.dt.date.astype("string")
        train = train[train["day"] < test_start]
        span_frame = pd.DataFrame(
            [{"station": s, "first": f, "last": la} for s, (f, la) in spans.items()]
        )
        span_frame["station"] = span_frame.station.astype("string")
        train = train.merge(span_frame, on="station")
        train = train[(train["day"] >= train["first"]) & (train["day"] <= train["last"])]
        if not train.empty:
            keys = nyc_keys(pd.DatetimeIndex(train.window))
            totals = (
                pd.DataFrame(
                    {
                        "station": train.station.values,
                        "weekday": keys.weekday.values,
                        slot: keys[slot].values,
                        "total": train.n.values,
                    }
                )
                .groupby(["station", "weekday", slot], observed=True)
                .total.sum()
                .reset_index()
            )
    log(f"Baseline-train {horizon}min: counting eligible windows per key")
    days = _span_days(
        spans, end_bound=(pd.Timestamp(test_start) - pd.Timedelta(days=1)).date().isoformat()
    )
    day_counts = (
        days.assign(weekday=days.day.dt.weekday.astype("int16"))
        .groupby(["station", "weekday"], observed=True)
        .size()
        .reset_index(name="n_days")
    )
    slots = pd.DataFrame({slot: pd.Series(range(n_slots), dtype="int16")})
    eligible = day_counts.merge(slots, how="cross")
    merged = eligible.merge(totals, on=["station", "weekday", slot], how="left")
    merged["total"] = merged.total.fillna(0)
    return {
        (str(station), int(weekday), int(slot_n)): float(total) / int(n_days)
        for station, weekday, slot_n, n_days, total in zip(
            merged.station, merged.weekday, merged[slot], merged.n_days, merged.total
        )
    }


def evaluate(events, spans, means, fallback, test_start, horizon):
    """MAE over eligible test windows; empty-but-eligible counts as zero."""
    slot = f"slot_{horizon}"
    n_slots = 24 * 60 // horizon
    log(f"Baseline-score {horizon}min: building test grid")
    days = _span_days(spans, start_bound=test_start)
    if days.empty:
        return {"mae": None, "test_windows": 0, "fallback_rate": None}
    offsets = pd.DataFrame(
        {"offset": pd.to_timedelta([horizon * w for w in range(n_slots)], unit="m")}
    )
    grid = days.merge(offsets, how="cross")
    grid["window"] = pd.DatetimeIndex(grid.day.values + grid.offset.values, tz="UTC")
    log(f"Baseline-score {horizon}min: scoring {len(grid):,} windows")
    keys = nyc_keys(pd.DatetimeIndex(grid.window))
    pred = pd.Series(
        list(zip(grid.station.astype(str), keys.weekday.astype(int), keys[slot].astype(int)))
    ).map(means)
    used_fallback = int(pred.isna().sum())
    pred = pred.fillna(fallback).astype("float64")
    if not events.empty:
        lookup = (
            events.assign(station=events.station.astype("string"))
            .set_index(["station", "window"])
            .n
        )
        actual = (
            pd.MultiIndex.from_arrays(
                [grid.station.astype("string"), pd.DatetimeIndex(grid.window)]
            )
            .map(lookup)
            .fillna(0)
            .astype("float64")
        )
    else:
        actual = pd.Series(0.0, index=range(len(grid)))
    return {
        "mae": float((actual.values - pred.values).__abs__().sum() / len(grid)),
        "test_windows": len(grid),
        "fallback_rate": used_fallback / len(grid),
    }


def run_session2(trips_dir, mapping_path, out_dir, months):
    """Scan months, write 30/60-min Parquet, report baseline v0 MAE."""
    trips_dir, out_dir = Path(trips_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    matched = matched_ids(mapping_path)
    log(f"Matched stations: {len(matched):,}")
    manifest = {
        "generated_at": utc_now(),
        "months": [],
        "limitations": [
            "Trip times are America/New_York local converted to UTC; DST-ambiguous rows excluded.",
            "Only verified matched IDs are kept; unmatched and missing IDs are excluded, not guessed.",
            "Eligibility is the first/last observed trip day per station (operating history unknown).",
            "Windows outside a station span are absent, not zero; emitted rows are eligible.",
        ],
    }
    combined = {(h, f): [] for h in (30, 60) for f in ("dep", "arr")}
    span_list = []
    for month in months:
        archive = trips_dir / f"{month}-citibike-tripdata.zip"
        if not archive.exists():
            raise FileNotFoundError(f"Missing {archive}; download it first")
        log(f"Stage scan: starting {archive.name}")
        evidence, events, spans = scan_month(archive, matched)
        log(f"Stage scan: done {archive.name} ({evidence['counts']['rows']:,} rows)")
        manifest["months"].append(evidence)
        for key, frame in events.items():
            combined[key].append(frame)
        span_list.append(spans)
    spans = merge_spans(span_list)
    manifest["stations"] = len(spans)
    log(f"Stage merge: {len(spans):,} stations with spans")
    month_frames = {}
    for key, frames in combined.items():
        log(f"Stage merge: combining {key}")
        non_empty = [f for f in frames if not f.empty]
        month_frames[key] = (
            pd.concat(non_empty).groupby(["station", "window"], observed=True).n.sum().reset_index()
            if non_empty
            else pd.DataFrame(
                {
                    "station": pd.Series(dtype="string"),
                    "window": pd.Series(dtype="datetime64[ns, UTC]"),
                    "n": pd.Series(dtype="int32"),
                }
            )
        )
    global_max = max(last for _, last in spans.values())
    manifest["test_start"] = split_day(global_max)
    pd.DataFrame(
        [{"station_id": s, "first_day": f, "last_day": la} for s, (f, la) in sorted(spans.items())]
    ).to_csv(out_dir / "station_spans.csv", index=False)
    month_tags = sorted(
        {(w.year, w.month) for frame in month_frames.values() for w in frame.window}
    )
    for horizon in (30, 60):
        for year, month in month_tags:
            part_dir = out_dir / f"targets_{horizon}min" / f"year={year}" / f"month={month:02d}"
            log(f"Stage grid: writing {horizon}min {year}-{month:02d}")
            files = write_month_partition(
                month_frames[(horizon, "dep")],
                month_frames[(horizon, "arr")],
                spans,
                year,
                month,
                horizon,
                part_dir,
            )
            log(f"Stage grid: wrote {horizon}min {year}-{month:02d} in {files} files")
    baseline = {"generated_at": utc_now(), "test_days": TEST_DAYS, "horizons": {}}
    for horizon in (30, 60):
        horizon_result = {}
        for flow in ("dep", "arr"):
            name = "departures" if flow == "dep" else "arrivals"
            log(f"Stage baseline: train {horizon}min {name}")
            events = month_frames[(horizon, flow)]
            means = train_key_means(events, spans, manifest["test_start"], horizon)
            fallback = float(events.n.mean()) if not events.empty else 0.0
            log(f"Stage baseline: score {horizon}min {name} ({len(means):,} train keys)")
            result = evaluate(events, spans, means, fallback, manifest["test_start"], horizon)
            result["train_keys"] = len(means)
            horizon_result[name] = result
        baseline["horizons"][f"{horizon}min"] = horizon_result
        log(f"Baseline {horizon}min: {json.dumps(horizon_result)}")
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (out_dir / "baseline_v0.json").write_text(json.dumps(baseline, indent=2) + "\n")
    log("Stage done: manifest.json and baseline_v0.json written")
    return {"manifest": manifest, "baseline": baseline}
