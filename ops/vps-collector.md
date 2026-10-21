# VPS collector runbook (Windows 10)

Purpose: keep `bikeflow collect` running 24/7 independent of the laptop.

## One-time setup

```powershell
winget install Python.Python.3.12 Git.Git --accept-source-agreements
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
git clone https://github.com/abdelmoghit-elasraoui/BikeFlow-AI.git C:\BikeFlow
cd C:\BikeFlow
uv sync --locked
[System.Environment]::SetEnvironmentVariable('BIKEFLOW_DATA_DIR','C:\BikeFlow\data','Machine')
Start-Service w32time; w32tm /resync   # a drifting clock breaks TLS
```

Verify once: `cd C:\BikeFlow; uv run bikeflow collect --count 1 --interval 60`

## Scheduled task

Task Scheduler -> Create Task:

- General: `BikeFlowCollector`, "Run whether user is logged on or not".
- Triggers: At startup.
- Actions: `powershell.exe -NoProfile -Command "cd C:\BikeFlow; uv run bikeflow collect --count 0 --interval 60 >> C:\BikeFlow\data\collect.log 2>&1"`, start in `C:\BikeFlow`.
- Settings: stop-if-runs-longer unchecked; restart every 1 minute, 999 attempts.

## Health checks

```powershell
cd C:\BikeFlow
Get-ScheduledTask -TaskName BikeFlowCollector | Select State
uv run bikeflow coverage
(Get-ChildItem C:\BikeFlow\data\raw\gbfs -Recurse -Filter *.json.gz | Measure-Object).Count
Get-Content C:\BikeFlow\data\collect.log -Tail 3
```

Healthy: State Running; newest file under 2 minutes old; polls growing ~60/hour;
`errors` unchanged; `gaps_over_10_minutes` not growing.

## Retention (60GB SSD)

Weekly task deletes snapshots older than 90 days:

```powershell
Get-ChildItem C:\BikeFlow\data\raw\gbfs -Recurse -Filter *.json.gz |
  Where-Object LastWriteTime -lt (Get-Date).AddDays(-90) | Remove-Item
```

## Pull a mirror to the laptop

```sh
rsync -avz --partial USER@VPS:'C:/BikeFlow/data/raw/gbfs/' data/vps-mirror/
uv run bikeflow coverage --raw-root data/vps-mirror
```

Do this weekly so a VPS loss costs a week, not the semester.

## Known gotchas

- PowerShell aliases `curl` to `Invoke-WebRequest`; use `curl.exe` for HTTP tests.
- `uv run` only resolves `bikeflow` inside `C:\BikeFlow`.
- `LastTaskResult 267009` (0x41301) means "currently running", not failure.
- Keep the VPS from auto-shutting down on RDP disconnect.
