# Run the validation-run watchdog via Task Scheduler (every 5 minutes).
# Short-lived: completes in seconds, so Task Scheduler's process teardown
# after completion is irrelevant. Output goes to storage\watchdog.log.
$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo '.venv\Scripts\python.exe'
$storage = Join-Path $repo 'storage'
New-Item -ItemType Directory -Force -Path $storage | Out-Null
& $python -u (Join-Path $repo 'scripts\watchdog.py') *>> (Join-Path $storage 'watchdog.log')
