@echo off
rem ============================================================
rem  Polymarket Arb Bot - PAPER RUN launcher
rem  Double-click this file, then LEAVE THIS WINDOW OPEN.
rem  Closing the window stops the bot. If it dies on its own,
rem  the watchdog (runs every 5 min) sends a Telegram alert
rem  within 15 minutes.
rem ============================================================
title Polymarket Arb Bot (PAPER) - leave this window open
cd /d "%~dp0"
rem -u = unbuffered. 2>&1 | Tee-Object keeps the dashboard visible
rem in this window AND appends everything to storage\paper_run.log
rem (which the watchdog and analysis scripts read).
.venv\Scripts\python.exe -u main.py 2>&1 | powershell -NoProfile -Command "$input | Tee-Object -FilePath 'storage\paper_run.log' -Append"
echo.
echo Bot stopped. You can close this window.
pause
