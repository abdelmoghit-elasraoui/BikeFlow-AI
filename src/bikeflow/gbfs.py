"""Discover feeds and preserve source payloads without assuming a fixed URL/version."""

import json
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_URL = "https://gbfs.citibikenyc.com/gbfs/gbfs.json"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def get_json(url, attempts=3, timeout=25):
    if not url.startswith("https://"):
        raise ValueError("Only HTTPS data sources are supported")
    for attempt in range(attempts):
        try:
            req = Request(url, headers={"User-Agent": "BikeFlowAI/0.1 academic data collector"})
            with urlopen(req, timeout=timeout) as response:
                payload = json.load(response)
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
                raise ValueError("Feed must contain an object data field")
            return {"url": url, "retrieved_at": utc_now(), "payload": payload}
        except (HTTPError, URLError, TimeoutError, ValueError, OSError):
            if attempt + 1 == attempts:
                raise
            time.sleep(2**attempt)


def discover(envelope):
    data = envelope["payload"]["data"]
    if "feeds" in data:
        feeds = data["feeds"]
    else:
        locale = data.get("en") or next(
            (v for v in data.values() if isinstance(v, dict) and "feeds" in v), None
        )
        if locale is None:
            raise ValueError("Discovery has no feeds")
        feeds = locale["feeds"]
    result = {f["name"]: f["url"] for f in feeds}
    for required in ("station_information", "station_status"):
        if required not in result:
            raise ValueError(f"Discovery missing {required}")
    return result


def stations(envelope):
    rows = envelope["payload"]["data"].get("stations")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Expected a nonempty station list")
    ids = [s.get("station_id") for s in rows]
    if any(not isinstance(s, str) or not s for s in ids) or len(set(ids)) != len(ids):
        raise ValueError("Missing or duplicate station IDs")
    return rows


def epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    raise ValueError("Invalid source timestamp")


def validate_status(row):
    problems = []
    for field in ("num_bikes_available", "num_docks_available"):
        # GBFS 3 uses num_vehicles_available instead of num_bikes_available.
        value = row.get(field, row.get("num_vehicles_available") if "bikes" in field else None)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            problems.append(f"invalid_{field}")
    for field in ("is_installed", "is_renting", "is_returning"):
        if row.get(field) not in (0, 1, False, True):
            problems.append(f"invalid_{field}")
    try:
        epoch(row.get("last_reported"))
    except (ValueError, TypeError):
        problems.append("invalid_last_reported")
    return problems
