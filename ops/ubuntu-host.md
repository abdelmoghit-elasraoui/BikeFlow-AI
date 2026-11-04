# Ubuntu host runbook

The collector and API run on a London Ubuntu 26.04 VPS (4GB RAM, 50GB NVMe +
700GB HDD). Replaces the earlier Windows host.

## Layout

```
/srv/bikeflow/            code, venv, SQLite, models
/srv/bikeflow/data/raw/gbfs -> /data/gbfs   (symlink)
/data/gbfs/               raw snapshots (700GB disk)
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
Host bikeflow-ubuntu
  HostName 51.195.191.88
  Port 4373
  User root
```

Tunnel and check:

```bash
ssh -f -N -L 8000:127.0.0.1:8000 bikeflow-ubuntu
curl -s localhost:8000/freshness | python3 -m json.tool
```

## Retention

No automatic deletion. All raw snapshots are kept so the history is complete for
later model evaluation and retraining.

Growth is about 200MB per day (~6GB per month, ~72GB per year), against 690GB
free. At that rate the disk lasts roughly nine years. Watch `df -h /data`
monthly; if space ever becomes tight, archive older years to object storage
instead of deleting.

## Migration notes (4 November 2026)

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
