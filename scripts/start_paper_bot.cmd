@echo off
rem Launcher for the continuous paper run, invoked by Windows Task Scheduler.
rem cd to the repo root (%~dp0 = this script's folder; .. = repo root), then
rem start the bot DETACHED with its output appended to storage\paper_run.log.
rem
rem `start "" /b` + `-u` is deliberate: cmd applies the redirect to the started
rem process itself and returns immediately, so the scheduled task completes
rem while the bot keeps running as a detached process. -u makes Python's
rem stdout/stderr unbuffered so the log is written live even though it is not
rem a console. (A plain `python main.py >> log 2>&1` line loses all output —
rem and often the process itself — when the parent cmd runs under the Task
rem Scheduler's non-console environment.)
cd /d "%~dp0\.."
start "" /b .venv\Scripts\python.exe -u main.py >> storage\paper_run.log 2>&1
