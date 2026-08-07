@echo off
cd /d "%~dp0.."
echo Starting Command Center API on http://127.0.0.1:8787 ...
echo Leave this window open. Close it to stop the API.
echo.
".venv\Scripts\python.exe" -m uvicorn command_center.api.main:app --port 8787
pause
