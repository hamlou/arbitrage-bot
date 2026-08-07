' Polymarket arb bot watchdog loop.
' Runs hidden at every logon (Startup folder) because Task Scheduler on this
' machine silently fails to execute tasks (service runs, tasks report success,
' nothing ever runs).
' Every 5 minutes: run scripts\watchdog.py, which alerts Telegram (deduped to
' once per hour) if the bot has been silent for more than 15 minutes.
Option Explicit
Dim sh, repo, python, cmd
Set sh = CreateObject("WScript.Shell")
repo = "C:\Users\hp\Desktop\polymarket-arb-bot (1)\polymarket-arb-bot"
python = repo & "\.venv\Scripts\python.exe"
Do
    cmd = "cmd /c cd /d """ & repo & """ && """ & python & """ -u scripts\watchdog.py >> storage\watchdog.log 2>&1"
    sh.Run cmd, 0, True   ' 0 = hidden window, True = wait for it to finish
    WScript.Sleep 300000  ' 5 minutes
Loop
