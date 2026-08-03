"""
Launch `python main.py` as a persistent, fully detached background process on
Windows, owned by the OS Task Scheduler (schtasks) rather than by whatever
terminal/shell spawned this script.

Why this exists: a plain `nohup ... &` / subprocess.Popen background job gets
torn down when its parent shell exits (job objects / console teardown). A
scheduled task is created and immediately triggered, so the bot becomes a
child of the Task Scheduler service and survives for the full continuous run.

Usage:
    python scripts/launch_paper_run.py
    python scripts/launch_paper_run.py --task-name PolymarketArbBotPaper
    python scripts/launch_paper_run.py --stop

--stop terminates the task (kills the running bot) without creating anything.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
MAIN = REPO_ROOT / "main.py"
LOG = REPO_ROOT / "storage" / "paper_run.log"
LAUNCHER = REPO_ROOT / "scripts" / "start_paper_bot.cmd"

# The task runs a tiny batch launcher (scripts/start_paper_bot.cmd) rather
# than an inline command: schtasks /tr with quotes, &&, parentheses in the
# path, and redirection fails with a generic COM error (0x80004005). A .cmd
# file avoids every quoting edge case and is also handy for manual restarts.


def _run(args: list[str]) -> None:
    subprocess.run(args, check=True, capture_output=True, text=True)


def create_and_start(task_name: str) -> None:
    # /f overwrites any stale task with the same name; /sc once + /st midnight
    # then /run triggers it immediately. /RL LIMITED keeps it out of admin
    # territory — the bot needs no privileges.
    # The repo path contains spaces ("polymarket-arb-bot (1)"), so the task
    # command line MUST be quoted — an unquoted /tr makes Task Scheduler split
    # on the space and fail with ERROR_FILE_NOT_FOUND (0x80070002).
    quoted = f'"{LAUNCHER}"'
    _run([
        "schtasks", "/create", "/tn", task_name,
        "/tr", quoted,
        "/sc", "once", "/st", "23:59",
        "/rl", "LIMITED", "/f",
    ])
    _run(["schtasks", "/run", "/tn", task_name])
    print(f"Created + started scheduled task '{task_name}'. Log: {LOG}")


def stop(task_name: str) -> None:
    try:
        _run(["schtasks", "/end", "/tn", task_name])
        print(f"Sent stop to task '{task_name}'.")
    except subprocess.CalledProcessError:
        print(f"Task '{task_name}' was not running (or does not exist).")
    # Remove the task definition too, so a stale definition can't be
    # accidentally re-triggered later.
    try:
        _run(["schtasks", "/delete", "/tn", task_name, "/f"])
        print(f"Deleted task '{task_name}'.")
    except subprocess.CalledProcessError:
        print(f"Could not delete task '{task_name}' (may not exist).")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-name", default="PolymarketArbBotPaper")
    parser.add_argument("--stop", action="store_true", help="Stop + delete the task instead of starting it.")
    args = parser.parse_args()
    if args.stop:
        stop(args.task_name)
    else:
        create_and_start(args.task_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
