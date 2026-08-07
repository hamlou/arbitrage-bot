@echo off
cd /d "%~dp0ui"
echo Starting Command Center UI on http://localhost:3000 ...
echo Leave this window open. Close it to stop the UI.
echo.
if not exist node_modules (
  echo First run detected - installing dependencies (this takes a minute)...
  call npm install
  if errorlevel 1 (
    echo.
    echo npm install failed. Is Node.js installed? (node --version)
    pause
    exit /b 1
  )
)
npm run dev
pause
