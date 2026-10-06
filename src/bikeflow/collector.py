"""Raw gzip snapshots, SQLite observations, and explicit failure/coverage records."""

import gzip
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .gbfs import discover, epoch, get_json, stations, utc_now, validate_status


def connect(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "observations.sqlite")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS polls (
            id TEXT PRIMARY KEY, retrieved_at TEXT NOT NULL,
            source_updated TEXT, status_hash TEXT, repeated_source INTEGER,
            raw_path TEXT, station_count INTEGER, invalid_count INTEGER,
            stale_count INTEGER, ttl INTEGER
        );
        CREATE TABLE IF NOT EXISTS observations (
            poll_id TEXT NOT NULL, station_id TEXT NOT NULL,
            last_reported TEXT, eligible INTEGER NOT NULL, issues TEXT NOT NULL,
            payload TEXT NOT NULL, PRIMARY KEY(poll_id, station_id)
        );
        CREATE TABLE IF NOT EXISTS errors (
            retrieved_at TEXT NOT NULL, message TEXT NOT NULL
        );
    """)
    return db


def collect_once(root, discovery_url):
    root = Path(root)
    directory = get_json(discovery_url)
    feeds = discover(directory)
    information = get_json(feeds["station_information"])
    status = get_json(feeds["station_status"])
    metadata = {s["station_id"]: s for s in stations(information)}
    rows = stations(status)
    payload = status["payload"]
    source_time = epoch(payload.get("last_updated"))
    now = datetime.now(timezone.utc)
    ttl = payload.get("ttl", 60)
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 0:
        raise ValueError("Invalid feed TTL")
    stale_after = max(300, ttl * 3)
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    poll_id = now.strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex[:8]
    relpath = Path("raw/gbfs") / now.strftime("%Y/%m/%d") / f"{poll_id}.json.gz"
    target = root / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    bundle = {
        "poll_id": poll_id,
        "discovery": directory,
        "station_information": information,
        "station_status": status,
    }
    # A failed database write still leaves original payloads for recovery.
    with gzip.open(target, "wt", encoding="utf-8") as stream:
        json.dump(bundle, stream, separators=(",", ":"))
    observations = []
    invalid = stale = 0
    for row in rows:
        issues = validate_status(row)
        invalid += bool(issues)
        if row["station_id"] not in metadata:
            issues.append("missing_metadata")
        try:
            age = now.timestamp() - epoch(row.get("last_reported"))
            if age > stale_after:
                issues.append("stale_station")
                stale += 1
            elif age < -60:
                issues.append("future_station_timestamp")
        except (ValueError, TypeError):
            pass
        if now.timestamp() - source_time > stale_after:
            issues.append("stale_feed")
        elif source_time - now.timestamp() > 60:
            issues.append("future_feed_timestamp")
        active = all(row.get(k) for k in ("is_installed", "is_renting", "is_returning"))
        eligible = active and not issues
        observations.append(
            (
                poll_id,
                row["station_id"],
                str(row.get("last_reported")),
                int(eligible),
                json.dumps(issues),
                json.dumps(row),
            )
        )
    with closing(connect(root)) as db, db:
        previous = db.execute(
            "SELECT status_hash FROM polls ORDER BY retrieved_at DESC LIMIT 1"
        ).fetchone()
        repeated = bool(previous and previous[0] == fingerprint)
        db.execute(
            "INSERT INTO polls VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                poll_id,
                status["retrieved_at"],
                str(payload["last_updated"]),
                fingerprint,
                int(repeated),
                str(relpath),
                len(rows),
                invalid,
                stale,
                ttl,
            ),
        )
        db.executemany("INSERT INTO observations VALUES (?,?,?,?,?,?)", observations)
    return {
        "poll_id": poll_id,
        "stations": len(rows),
        "invalid": invalid,
        "stale": stale,
        "eligible": sum(r[3] for r in observations),
        "repeated_source": repeated,
        "ttl": ttl,
        "raw_path": str(target),
    }


def collect(root, discovery_url, count=1, interval=60):
    failures = 0
    attempt = 0
    while count == 0 or attempt < count:
        started = time.monotonic()
        effective_interval = interval
        try:
            result = collect_once(root, discovery_url)
            effective_interval = max(interval, result["ttl"])
            print(json.dumps(result), flush=True)
        except Exception as exc:
            failures += 1
            message = f"{type(exc).__name__}: {exc}"
            with closing(connect(root)) as db, db:
                db.execute("INSERT INTO errors VALUES (?,?)", (utc_now(), message))
            print(json.dumps({"error": message, "retrieved_at": utc_now()}), flush=True)
        attempt += 1
        if count == 0 or attempt < count:
            time.sleep(max(0, effective_interval - (time.monotonic() - started)))
    return failures


def coverage(root):
    with closing(connect(root)) as db, db:
        polls = db.execute(
            "SELECT retrieved_at,repeated_source,station_count,invalid_count,stale_count "
            "FROM polls ORDER BY retrieved_at"
        ).fetchall()
        errors = db.execute("SELECT retrieved_at,message FROM errors").fetchall()
    times = [datetime.fromisoformat(p[0]) for p in polls]
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    return {
        "polls": len(polls),
        "first_retrieval": polls[0][0] if polls else None,
        "last_retrieval": polls[-1][0] if polls else None,
        "repeated_source_polls": sum(p[1] for p in polls),
        "max_gap_seconds": max(gaps, default=None),
        "gaps_over_10_minutes": sum(g > 600 for g in gaps),
        "invalid_observations": sum(p[3] for p in polls),
        "stale_observations": sum(p[4] for p in polls),
        "errors": errors,
    }


def raw_coverage(root):
    """Measure retrieval coverage across separately downloaded hosted artifacts."""
    records = {}
    for path in Path(root).rglob("*.json.gz"):
        with gzip.open(path, "rt") as stream:
            bundle = json.load(stream)
        status = bundle["station_status"]
        records[bundle["poll_id"]] = status
    ordered = sorted(records.values(), key=lambda r: r["retrieved_at"])
    times = [datetime.fromisoformat(r["retrieved_at"]) for r in ordered]
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    fingerprints = [
        hashlib.sha256(json.dumps(r["payload"], sort_keys=True).encode()).hexdigest()
        for r in ordered
    ]
    return {
        "unique_polls": len(ordered),
        "first_retrieval": times[0].isoformat() if times else None,
        "last_retrieval": times[-1].isoformat() if times else None,
        "max_gap_seconds": max(gaps, default=None),
        "gaps_over_10_minutes": sum(g > 600 for g in gaps),
        "repeated_source_polls": sum(a == b for a, b in zip(fingerprints, fingerprints[1:])),
        "note": "Observed retrieval coverage only. Consult per-run health/logs for failures.",
    }
