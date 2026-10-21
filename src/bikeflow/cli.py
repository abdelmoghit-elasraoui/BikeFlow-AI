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
    train.add_argument("--models", type=Path, default=Path("data/models"))
    train.add_argument(
        "--mapping",
        type=Path,
        default=Path("reports/first-session/station_mapping.csv"),
    )
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
                run_training(args.targets, args.models, args.version, args.mapping),
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
