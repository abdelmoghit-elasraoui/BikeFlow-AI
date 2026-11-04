# Session 4 report — host migration and storage fix

Date: 4 November 2026

## 1. Tasks completed today

**Infrastructure migration.** Moved the collector and API from the Windows VPS to
a London Ubuntu 26.04 server (4GB RAM, 50GB NVMe, 700GB HDD). Formatted and
mounted the 700GB disk at `/data`, installed Python/uv/git, cloned the repo, and
recreated the virtual environment.

**Data transfer.** Transferred 8,135 raw GBFS snapshots (1.6GB) from the old host.
The first single-archive relay broke mid-stream, so the transfer was split per
day and every day was verified by file count. Exported the small index tables
(`polls` 8,023 rows, `errors` 124, `predictions` 14,689) to CSV and imported them
into a fresh database on the new host.

**Process supervision.** Replaced Windows Task Scheduler with systemd units
(`bikeflow-collector.service`, `bikeflow-api.service`). Both restart on failure
and start at boot. SSH is key-only; the API binds to `127.0.0.1:8000` and is
reached from the Mac through an SSH tunnel.

**Storage fix.** Diagnosed an 11.2GB SQLite database: an `observations` table
stored one row per station per poll, including each station's full JSON payload,
duplicating the 1.6GB raw archive. Removed the table from the schema; the
collector now stores only poll metadata (including eligible/invalid/stale
counts), errors, and predictions. The API reads station state from the newest raw
snapshot. Database size dropped to 18MB for the same history.

**Retention policy.** Removed the automatic 90-day deletion job at the user's
request. All raw history is kept; growth is about 200MB/day against 690GB free.

**Documentation.** Added an Ubuntu host runbook, updated the project guide with a
full work log and a 25-entry issue register, and refreshed the eligibility and
README sections to match the new storage design.

## 2. Tangible execution proofs

- Latest commits: `a8c4291` (remove duplicated observations), `3150e9e` (docs),
  `ce6581e` (keep all raw snapshots), `67bbaf4` (Ubuntu runbook),
  `dffea9b` (work log and issue register).
- Database size: **11.2GB → 18MB** for 8,023 polls.
- Raw archive: **8,135 files, 1.6GB**, all preserved, growing every minute.
- Services: `bikeflow-collector` and `bikeflow-api` both `active` and `enabled`.
- API `/freshness` returned `age_seconds` under 60, `stale: false`, 2,518
  stations.
- Tests: `uv run pytest -q` → 16 passed; ruff clean.

## 3. Technical hurdles and fixes

- **Collection had been dead since 24 October.** The old Windows disk was full
  (`No space left on device`), producing 124 recorded errors. The outage appears
  in `coverage` as a maximum gap of ~967,550 seconds. Fixed by migrating to the
  700GB host.
- **The SQLite file was locked during archiving.** The collector held it open, so
  `tar` failed with `Permission denied`. Stopped both scheduled tasks before
  exporting; the first archive attempt was discarded.
- **The single-archive relay died with `Broken pipe`.** Both SSH connections
  dropped mid-transfer and left a truncated archive. Split the transfer per day
  and verified counts after each.
- **SSH password login to Ubuntu was denied.** Cloud images disable password auth
  for root. Used the Mac's existing key and relayed data through it instead of
  enabling passwords.
- **systemd failed with `status=203/EXEC`.** The virtual environment pointed at a
  Python installation under `/root`, unreadable by the service user. Copied the
  runtime to `/opt/uv-python`, recreated the venv, and fixed ownership.
- **LightGBM failed to import on Ubuntu.** Missing OpenMP runtime; installed
  `libgomp1`.
- **Git refused to pull.** “Dubious ownership” because the repo is owned by the
  service user; added `safe.directory` for root.

## 4. Priority goal before the next session

- Start Sprint 2: React dashboard scaffold and station map on top of the existing
  API.
- Add retention or deduplication for the `predictions` table, which grows by
  about 2,500 rows per `/queue` call.
- Extend historical trips toward 12 months and add the weather join, as recorded
  in the Sprint 2 scope.

## 5. Screenshots

1. `systemctl is-active bikeflow-collector bikeflow-api` → both active.
2. `du -h data/observations.sqlite` → 18M.
3. `find /data/gbfs -name '*.json.gz' | wc -l` → 8,135+.
4. `curl -s localhost:8000/freshness` → `stale: false`, age under 60s.
5. GitHub Projects board with the migration and storage tickets in Done.
