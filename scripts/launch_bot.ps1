# Launch the paper bot detached, for Windows Task Scheduler.
# Task Scheduler uses CreateProcess, which cannot execute .cmd files directly;
# a PowerShell script invoked via powershell.exe (a real executable) works.
#
# Usage (schtasks):
#   /tr "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden
#        -File \"C:\...\polymarket-arb-bot\scripts\launch_bot.ps1\""
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location -Path $repo

$python = Join-Path $repo '.venv\Scripts\python.exe'
$storage = Join-Path $repo 'storage'
New-Item -ItemType Directory -Force -Path $storage | Out-Null
$stdout = Join-Path $storage 'paper_run_out.log'   # dashboard ANSI frames
$stderr = Join-Path $storage 'paper_run.log'       # bot logging (stderr) - watchdog key

if (-not (Test-Path $python)) {
    Add-Content -Path (Join-Path $storage 'launcher_error.log') -Value "$(Get-Date -Format o) python.exe not found at $python"
    exit 1
}

# start "" /b equivalent: Start-Process detaches; the process outlives this
# script and the task. -u = unbuffered so the log is written live.
Start-Process -FilePath $python `
    -ArgumentList @('-u', 'main.py') `
    -WorkingDirectory $repo `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -WindowStyle Hidden
