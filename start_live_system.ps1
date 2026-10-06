# Starts the two processes the Live Scan page needs.
#
# They are separate servers and depend on each other: MediaMTX validates every drone publish
# against Django (/api/mediamtx/auth/), and Django polls MediaMTX (:9997) to decide whether to
# show LIVE. Running only one of them makes the dashboard report "Streaming server offline".
#
# Usage:  .\start_live_system.ps1
# Stop:   close the MediaMTX window, then press Ctrl+C here.

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

$mediamtxExe = Join-Path $root "deployment\mediamtx_bin\mediamtx.exe"
$mediamtxCfg = Join-Path $root "deployment\mediamtx.yml"
$venvPython = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $mediamtxExe)) { throw "MediaMTX not found at $mediamtxExe" }
if (-not (Test-Path $mediamtxCfg)) { throw "MediaMTX config not found at $mediamtxCfg" }

$python = if (Test-Path $venvPython) { $venvPython } else { "python" }

# Don't start a second copy: MediaMTX would fail to bind 1935/8888/9997 and exit.
if (Get-Process -Name "mediamtx" -ErrorAction SilentlyContinue) {
    Write-Host "MediaMTX is already running." -ForegroundColor Yellow
} else {
    Write-Host "Starting MediaMTX in a new window..." -ForegroundColor Cyan
    # Its own window so its log stays readable and closing it stops only the stream server.
    Start-Process -FilePath $mediamtxExe -ArgumentList $mediamtxCfg `
        -WorkingDirectory (Join-Path $root "deployment")
}

# Give it a moment to bind, then confirm the control API answers - this is the exact endpoint
# the dashboard polls, so if it fails here the page will say "Streaming server offline".
Start-Sleep -Seconds 2
try {
    Invoke-RestMethod -Uri "http://127.0.0.1:9997/v3/paths/list" -TimeoutSec 5 | Out-Null
    Write-Host "MediaMTX API is responding on :9997" -ForegroundColor Green
} catch {
    Write-Host "MediaMTX API did not respond on :9997 - check its window for errors." -ForegroundColor Red
}

Write-Host ""
Write-Host "Live Scan page: http://127.0.0.1:8000/predictions/predict/" -ForegroundColor Cyan
Write-Host "Starting Django (Ctrl+C to stop)..." -ForegroundColor Cyan
Write-Host ""

# Must be 0.0.0.0 so the phone running DJI Fly can reach this machine over the LAN, and must be
# port 8000 because deployment/mediamtx.yml posts auth callbacks to 127.0.0.1:8000.
& $python (Join-Path $root "manage.py") runserver 0.0.0.0:8000
