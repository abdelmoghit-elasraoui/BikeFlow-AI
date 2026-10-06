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
