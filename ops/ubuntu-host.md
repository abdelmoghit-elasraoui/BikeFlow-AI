# Ubuntu host runbook

The collector and API run on a Sydney Ubuntu 26.04 VPS (2 vCPU, 3GB RAM,
20GB NVMe + 100GB HDD). Replaces the earlier London host, which replaced the
Windows host. Chosen for cost: only three months of semester remain and the
measured load is small.

## Layout

```
/srv/bikeflow/            code, venv, SQLite, models
/srv/bikeflow/data/raw/gbfs -> /data/gbfs   (symlink)
/data/gbfs/               raw snapshots (100GB disk)
/data/sidace_harvester/   separate harvested document set
```

The database stays on the NVMe (`/srv/bikeflow/data/observations.sqlite`);
raw snapshots go to the HDD. The HDD is mounted by UUID in `/etc/fstab`:

```
/dev/vdb /data ext4 defaults,noatime 0 2
```

## Services

Both run as the `bikeflow` system user and start at boot.

```bash
systemctl status bikeflow-collector --no-pager
systemctl status bikeflow-api --no-pager
journalctl -u bikeflow-collector -n 20 --no-pager
journalctl -u bikeflow-api -n 20 --no-pager
```

- `bikeflow-collector.service` runs `collect --count 0 --interval 60`.
- `bikeflow-api.service` runs `serve --version v1 --host 127.0.0.1 --port 8000`.

Both `Restart=always` with a 10s delay.

## Why the venv is special

`uv` installed Python under `/root/.local/share/uv/python`, which the service
user cannot read. The Python tree was copied to `/opt/uv-python` and the venv
recreated with:

```bash
uv venv --python /opt/uv-python/cpython-3.12.16-linux-x86_64-gnu/bin/python3.12
uv sync --locked
```

System Python is 3.14, outside the project's `>=3.12,<3.14` range.

`libgomp1` is required by LightGBM: `apt install -y libgomp1`.

## Access from the Mac

`~/.ssh/config`:

```
Host bikeflow
  HostName 139.99.145.71
  Port 3137
  User root
```

Tunnel and check:

```bash
ssh -f -N -L 8000:127.0.0.1:8000 bikeflow
curl -s localhost:8000/freshness | python3 -m json.tool
```

## Retention

No automatic deletion. All raw snapshots are kept so the history is complete for
later model evaluation and retraining.

Growth is about 300MB per day (~9GB per month), against 82GB free on the
100GB disk. Three months of collection needs roughly 27GB, leaving room for the
9.2GB `sidace_harvester` set and snapshots. Watch `df -h /data` monthly.

## Migration notes (12 November 2026, London → Sydney)

- New host: 2 vCPU, 3GB RAM, 20GB root + 100GB data disk (98GB usable).
- Data copied server-to-server with rsync using a dedicated key generated on the
  new host: raw snapshots (19,399 files, 3.8GB), `sidace_harvester` (9.2GB),
  SQLite DB, models, and processed targets.
- Services were installed with the same systemd units and started after the
  copy; the old London services were stopped and disabled.
- No data loss: the poll history is continuous (gap only for the minutes during
  the switch).
- Mac SSH alias `bikeflow` now points to the Sydney host (port 3137); the London
  host is `bikeflow-old` for rollback.

## Migration notes (4 November 2026, Windows → London)

- Raw snapshots (8,135 files, 1.6GB) were moved day by day; checksum-free but
  file counts match the source.
- The SQLite `observations` table was **not** migrated: it was 11.2GB because it
  repeats each station's JSON payload per poll. `polls` (8,023), `errors` (124),
  and `predictions` (14,689) were exported to CSV and imported into a fresh DB.
  The table has since been removed from the collector entirely; the API reads
  station state from the newest raw snapshot. The database is 18MB for the same
  history. See `docs/PROJECT_GUIDE.md` section 3.6.
- Historical observations remain available in the raw archive; the database
  restarts from 4 November.
- Collection was down from 24 October (Windows disk full) to 4 November. The
  outage is visible in `coverage` as `max_gap_seconds` (~967,550s) and in the
  `errors` table (`No space left on device`).
