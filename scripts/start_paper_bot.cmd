@echo off
rem Launcher for the continuous paper run, invoked by Windows Task Scheduler.
rem cd to the repo root (%~dp0 = this script's folder; .. = repo root), then
rem run the bot appending output to storage\paper_run.log.
cd /d "%~dp0\.."
.venv\Scripts\python.exe main.py >> storage\paper_run.log 2>&1
