' Polymarket arb bot watchdog loop.
' Runs hidden at every logon (Startup folder) because Task Scheduler on this
' machine silently fails to execute tasks (service runs, tasks report success,
' nothing ever runs).
' Every 5 minutes: run scripts\watchdog.py, which alerts Telegram (deduped to
' once per hour) if the bot has been silent for more than 15 minutes.
'
' The repo path is derived from THIS script's own location
' (<repo>\scripts\watchdog_loop.vbs) rather than a hand-typed absolute path
' (reviewed 2026-08-07): the watchdog exists to catch silent failures, so it
' must not become one if the folder is renamed, moved, or re-cloned. WScript.
' ScriptFullName is the full path to this file; the repo is its parent's
' parent.
Option Explicit
Dim fso, sh, repo, python, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
repo = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
python = repo & "\.venv\Scripts\python.exe"
Do
    cmd = "cmd /c cd /d """ & repo & """ && """ & python & """ -u scripts\watchdog.py >> storage\watchdog.log 2>&1"
    sh.Run cmd, 0, True   ' 0 = hidden window, True = wait for it to finish
    WScript.Sleep 300000  ' 5 minutes
Loop
