@echo off
REM ============================================================
REM  Polymarket Arbitrage Bot - Paper Mode Launcher
REM  Double-click this file to start the bot in PAPER mode.
REM  Close the window to stop the bot.
REM  (Do NOT close it if you want it to keep trading.)
REM ============================================================
cd /d "%~dp0"

IF NOT EXIST ".venv\Scripts\python.exe" (
    echo.
    echo  ERROR: Python environment not found.
    echo  Run these first:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo.
echo  Starting Polymarket Arbitrage Bot (PAPER MODE)...
echo  Leave this window open. Close it to stop.
echo.
.venv\Scripts\python.exe main.py

echo.
echo  The bot has stopped. This window will close in 10 seconds...
timeout /t 10
