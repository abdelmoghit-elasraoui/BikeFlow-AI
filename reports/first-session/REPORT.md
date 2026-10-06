# BikeFlow first session results

Completed on 6 October 2026. This session implements steps 1–7 of the first-session
plan. Forecast models, weather joins, API, and dashboard development have not started.

## Repository and environment

- Inspected the local repository and the empty existing GitHub repository. Preserved
  the original proposal, teacher documents, and plan.
- Connected `origin` to `https://github.com/abdelmoghit-elasraoui/BikeFlow-AI` and
  pushed the initial code to `main`.
- Established Python 3.12, a local `.venv`, locked dependencies with uv, an
  installable `bikeflow` CLI, `.env.example`, and Git exclusions for raw data,
  SQLite, environments, and secrets.

## Live collection

Three local polls succeeded, approximately one minute apart. Each returned 2,520
station records. The source TTL was 60 seconds. The database contains 7,560 station
observations, and SQLite's integrity check returned `ok`.

| Poll | Eligible stations | Stale station reports | Invalid availability counts |
| --- | ---: | ---: | ---: |
| First | 2,423 | 90 | 0 |
| Second | 2,422 | 91 | 0 |
| Third | 2,420 | 93 | 0 |

Eligibility also considers closure flags, feed freshness, future timestamps, and
missing metadata. Stale and closed rows remain archived for analysis.
There were no recorded request failures during this short local check.
No source response repeated during these polls; an offline test verifies repeated
response classification. A separate test verifies failure recording without
fabricated observations.

Raw compressed snapshots retain each original feed's retrieval time, feed time,
version/TTL, and station reporting timestamps. The source reporting time is distinct
from our HTTP retrieval time. See `local_collection_check.json` for coverage.

## Complete historical trip inspection

The local archive download was slow and was stopped. The complete February 2026
archive was downloaded and scanned on a GitHub-hosted runner, and its report,
station mapping, diagnostic table, and metadata snapshot were retrieved locally.
No partial archive was treated as a complete dataset.

[Successful full audit run](https://github.com/abdelmoghit-elasraoui/BikeFlow-AI/actions/runs/37459095318)

| Data | Result |
| --- | ---: |
| First CSV part | 1,000,000 rows |
| Second CSV part | 219,444 rows |
| Total trips | 1,219,444 |
| Missing start station IDs | 1,119 |
| Missing end station IDs | 8,058 |
| Invalid or ambiguous timestamps | 0 |
| Negative durations | 0 |
| Invalid endpoint coordinates | 0 |
| Unique trip station IDs | 2,277 |

All expected trip columns were present in both CSVs. Timestamps were interpreted in
America/New_York and converted to UTC. The earliest start is
`2026-01-31T04:24:24.773Z` (30 January in NYC) and the latest is
`2026-03-01T04:57:16.242Z` (28 February in NYC). The source archive therefore
includes some starts outside its nominal month. The next training ingestion must
explicitly filter by the intended local interval and deduplicate overlapping files.
This audit has not counted the out-of-month rows or verified ride-ID uniqueness.

The archive SHA-256 and complete machine-readable results are in `trip_audit.json`.

## Historical and live station mapping

Exact identifier candidates were located through GBFS `short_name` or `station_id`,
then checked against names and mean trip coordinates within 150 metres.

| Mapping category | Station identifiers |
| --- | ---: |
| Verified active match | 2,130 |
| Matching identifier but currently inactive | 74 |
| Unmatched | 71 |
| Changed name requiring review | 2 |
| Ambiguous identifier | 0 |
| Coordinate mismatch beyond 150 metres | 0 |

The two changed-name cases are abbreviations of “Queens Plaza North” to “Queens
Plaza N”, at unchanged coordinates. They are still flagged in the generated mapping
rather than silently reclassified. Inactive today does not imply inactive in February.
Unmatched historical stations require investigation; do not guess their live identity.
Current metadata does not establish historical capacity or opening/closing dates.
Full row-level evidence is in `station_mapping.csv`.

## Diagnostic station-time table

Created `data/processed/station_time_diagnostic.csv` locally with 20,664 observed
30-minute windows for the twenty busiest verified stations. Columns contain station
ID, UTC window start, departures, and arrivals. Departures use trip start time;
arrivals use trip end time. Verified unique station/window keys, nonnegative counts,
row count, and station count.

This is an initial data diagnostic. It does not fill unobserved windows, establish
station operating history, or serve as the final training dataset. The bulk table
is intentionally excluded from Git and can be regenerated or downloaded from the
audit run while the artifact remains available.

## Hosted collection

The `Collect station snapshots` workflow is active on the default branch and is
scheduled approximately every five minutes. A manually dispatched hosted run
successfully collected a real snapshot and uploaded its archive and health report.
That artifact was downloaded locally and checked. This proves the collector runs
independently of the development laptop; it does not yet establish long-term uptime.

[Successful initial hosted collection](https://github.com/abdelmoghit-elasraoui/BikeFlow-AI/actions/runs/37459090551)

Each full raw snapshot is approximately 200 KB compressed at current system size.
The artifact retention is seven days. At the configured schedule, raw storage can
approach 400 MB per week, before other artifacts. Scheduling delays/dropped runs,
account storage limits, and inactivity disablement remain operational limitations.
Review workflow failures and download artifacts before expiry. Do not rely on this
arrangement as a permanent semester archive; durable object storage and a continuous
worker are the next infrastructure task when the hosting account is selected.

The local three-poll process has completed. The hosted schedule is the continuing
collection mechanism. Per-run failures are visible in Actions and health artifacts;
`bikeflow coverage --raw-root data/hosted` measures observed gaps across downloads.

## Verification and remaining issues

Seven offline tests cover discovery versions, invalid counts, DST interpretation,
ambiguous/distant station matching, raw payload preservation/repeated responses,
failure recording, and scanning every CSV part with correct arrival/departure times.
Linting and formatting checks pass. The initial GitHub CI run also passed; the
expanded tests are included in the final session commit.

Before model training, handle missing station IDs, archive-month boundaries,
cross-file duplicate rides, unmatched/name-changed identifiers, station lifecycle,
feature availability at inference, and sufficient live-state collection coverage.
The from-scratch course eligibility exception remains a question for the teacher;
technical implementation does not resolve that administrative requirement.
