import argparse
import json
import os
from pathlib import Path

from .collector import collect, coverage, raw_coverage
from .gbfs import DEFAULT_URL


def main():
    parser = argparse.ArgumentParser(description="BikeFlow first-session data tools")
    parser.add_argument(
        "--data-dir", type=Path, default=Path(os.getenv("BIKEFLOW_DATA_DIR", "data"))
    )
    sub = parser.add_subparsers(dest="command", required=True)
    poll = sub.add_parser("collect", help="Collect GBFS; count=0 runs until interrupted")
    poll.add_argument("--count", type=int, default=1)
    poll.add_argument("--interval", type=int, default=60)
    poll.add_argument("--url", default=os.getenv("BIKEFLOW_GBFS_URL", DEFAULT_URL))
    health = sub.add_parser("coverage")
    health.add_argument("--raw-root", type=Path, help="Summarize downloaded raw archives")
    audit = sub.add_parser("audit-trips", help="Scan every CSV in a monthly ZIP")
    audit.add_argument("archive", type=Path)
    audit.add_argument("--snapshot", type=Path, required=True)
    audit.add_argument("--report-dir", type=Path, default=Path("reports"))
    second = sub.add_parser("session2", help="Build 30/60-min targets and baseline v0")
    second.add_argument("--months", nargs="+", default=["202512", "202601", "202602"])
    second.add_argument(
        "--mapping", type=Path, default=Path("reports/first-session/station_mapping.csv")
    )
    second.add_argument("--out", type=Path, default=Path("data/processed/session2"))
    train = sub.add_parser("train", help="Train pooled LightGBM models and compare to baseline")
    train.add_argument("--version", default="v1")
    train.add_argument("--targets", type=Path, default=Path("data/processed/session2"))
    train.add_argument(
        "--months",
        nargs="+",
        default=["202512", "202601", "202602"],
        help="month tags recorded in metadata (defaults to the Sprint 1 slice)",
    )
    train.add_argument("--models", type=Path, default=Path("data/models"))
    train.add_argument(
        "--weather", action="store_true", help="Join hourly weather features (v3 and later)"
    )
    train.add_argument("--weather-cache", type=Path, default=Path("data/raw/weather"))
    train.add_argument(
        "--feature-groups",
        nargs="+",
        default=None,
        help="calendar weather holiday station (defaults from --weather)",
    )
    train.add_argument("--trees", type=int, default=600)
    train.add_argument("--offset", choices=("mean", "median"), default="mean")
    train.add_argument("--no-calibration", action="store_true")
    train.add_argument(
        "--mapping",
        type=Path,
        default=Path("reports/first-session/station_mapping.csv"),
    )
    live_features = sub.add_parser(
        "live-features", help="Build live-state features from archived GBFS snapshots"
    )
    live_features.add_argument("--version", default="v2")
    live_features.add_argument("--models", type=Path, default=Path("data/models"))
    live_features.add_argument("--start", required=True, help="First day, e.g. 2026-11-12")
    live_features.add_argument("--end", required=True, help="Day after the last, e.g. 2026-11-20")
    live_features.add_argument("--horizon", type=int, choices=(30, 60), default=30)
    live_features.add_argument("--out", type=Path, required=True)
    train_live = sub.add_parser(
        "train-live", help="Fit the live-state correction on the labelled trip/snapshot overlap"
    )
    train_live.add_argument("--version", default="v2")
    train_live.add_argument("--targets", type=Path, default=Path("data/processed/v2"))
    train_live.add_argument("--live", type=Path, required=True)
    train_live.add_argument("--models", type=Path, default=Path("data/models"))
    train_live.add_argument("--horizon", type=int, choices=(30, 60), default=30)
    train_live.add_argument("--flow", choices=("departures", "arrivals"), default="departures")
    weather_fetch = sub.add_parser(
        "weather-fetch", help="Cache historical hourly forecasts for training"
    )
    weather_fetch.add_argument("--start", required=True)
    weather_fetch.add_argument("--end", required=True)
    weather_fetch.add_argument("--cache", type=Path, default=Path("data/raw/weather"))
    weather_collect = sub.add_parser(
        "weather-collect", help="Archive the live hourly forecast for serving"
    )
    weather_collect.add_argument("--cache", type=Path, default=Path("data/weather"))
    serve = sub.add_parser("serve", help="Serve forecasts and the ranked queue")
    serve.add_argument("--version", default="v1")
    serve.add_argument("--models", type=Path, default=Path("data/models"))
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.command == "collect":
        if args.count < 0 or args.interval < 1:
            parser.error("count must be nonnegative and interval must be positive")
        raise SystemExit(bool(collect(args.data_dir, args.url, args.count, args.interval)))
    if args.command == "coverage":
        result = raw_coverage(args.raw_root) if args.raw_root else coverage(args.data_dir)
        print(json.dumps(result, indent=2))
    if args.command == "audit-trips":
        from .trips import audit_archive

        print(
            json.dumps(
                audit_archive(args.archive, args.snapshot, args.data_dir, args.report_dir), indent=2
            )
        )
    if args.command == "session2":
        from .session2 import run_session2

        print(
            json.dumps(
                run_session2(args.data_dir / "raw" / "trips", args.mapping, args.out, args.months),
                indent=2,
                default=str,
            )
        )
    if args.command == "train":
        from .model import run_training

        print(
            json.dumps(
                run_training(
                    args.targets,
                    args.models,
                    args.version,
                    args.mapping,
                    args.months,
                    weather=args.weather,
                    weather_cache=args.weather_cache,
                    feature_groups=args.feature_groups,
                    trees=args.trees,
                    offset_statistic=args.offset,
                    calibrate=not args.no_calibration,
                ),
                indent=2,
                default=str,
            )
        )
    if args.command == "live-features":
        from .live import build_live_features

        metadata = json.loads((args.models / args.version / "metadata.json").read_text())
        frame = build_live_features(
            args.data_dir, args.start, args.end, args.horizon, metadata["station_codes"]
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(args.out, index=False)
        print(
            json.dumps(
                {
                    "rows": len(frame),
                    "stations": int(frame.station_code.nunique()),
                    "windows": int(frame.window_start_utc.nunique()),
                    "out": str(args.out),
                }
            )
        )
    if args.command == "weather-fetch":
        from .weather import load

        frame = load(args.start, args.end, args.cache)
        print(
            json.dumps(
                {
                    "hours": len(frame),
                    "start": str(frame.weather_hour.min()),
                    "end": str(frame.weather_hour.max()),
                }
            )
        )
    if args.command == "weather-collect":
        from .weather import fetch_live

        frame = fetch_live(args.cache)
        print(
            json.dumps(
                {
                    "hours": len(frame),
                    "start": str(frame.weather_hour.min()),
                    "end": str(frame.weather_hour.max()),
                }
            )
        )
    if args.command == "train-live":
        from .live import train_live_correction

        print(
            json.dumps(
                train_live_correction(
                    args.targets,
                    args.live,
                    args.models,
                    args.version,
                    args.horizon,
                    args.flow,
                ),
                indent=2,
                default=str,
            )
        )
    if args.command == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(
            create_app(args.models / args.version, args.data_dir),
            host=args.host,
            port=args.port,
        )
