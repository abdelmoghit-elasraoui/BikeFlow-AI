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

## Local API over SSH tunnel

One-time: install OpenSSH Server on the VPS and the Microsoft VC++ runtime
(LightGBM needs `lib_lightgbm.dll` dependencies):

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd; Set-Service sshd -StartupType Automatic
New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22
curl.exe -L -o C:\vc_redist.x64.exe https://aka.ms/vs/17/release/vc_redist.x64.exe
C:\vc_redist.x64.exe /install /quiet /norestart
```

Admin accounts use `C:\ProgramData\ssh\administrators_authorized_keys` (not
`~/.ssh/authorized_keys`); ACL it to Administrators + SYSTEM and restart sshd.

Scheduled task `BikeFlowAPI` (OnStart, run-as Administrator) executes:

```text
cmd /c C:\BikeFlow\.venv\Scripts\bikeflow.exe --data-dir C:\BikeFlow\data serve
      --version v1 --models C:\BikeFlow\data\models --host 127.0.0.1 --port 8000
```

Note `--data-dir` is a global flag and must come before `serve`.

From the Mac:

```sh
ssh -f -N -L 8000:127.0.0.1:8000 bikeflow
curl -s localhost:8000/freshness | python3 -m json.tool
```

Use `127.0.0.1` in the tunnel, not `localhost`: Windows resolves `localhost` to
IPv6 while uvicorn binds IPv4, which produces an empty reply.

Stopping/updating the API:

```powershell
schtasks /end /tn BikeFlowAPI
schtasks /run /tn BikeFlowAPI
```

## Known gotchas

- PowerShell aliases `curl` to `Invoke-WebRequest`; use `curl.exe` for HTTP tests.
- `uv run` only resolves `bikeflow` inside `C:\BikeFlow`.
- `LastTaskResult 267009` (0x41301) means "currently running", not failure.
- Keep the VPS from auto-shutting down on RDP disconnect.
