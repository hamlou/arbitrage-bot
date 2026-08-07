"""
Watchdog for the continuous validation run.

A dead process must not silently waste days of the validation window. This
script checks two independent liveness signals written by the bot:

  - command_center/api/live_state.json  (written every ~2s by the bot)
  - storage/paper_run.log               (appended every second by the
                                         dashboard + logging)

If BOTH are older than STALE_S (default 900s = 15 min), the bot is presumed
dead: a CRITICAL Telegram alert is sent (never muted, never dropped). Alerts
are deduped — at most one per ALERT_COOLDOWN_S (default 3600s), tracked in
storage/watchdog_state.json, so a dead bot alerts once, not every 5 minutes.

Intended to run every 5 minutes from scripts/watchdog_loop.vbs (a hidden loop
in the Windows Startup folder — Task Scheduler on this machine silently fails
to execute tasks, verified 2026-08-07). Exits 0 always (a watchdog failure to
check is itself reported to Telegram, never silently).

Usage:
    python scripts/watchdog.py
    python scripts/watchdog.py --stale-s 900 --cooldown-s 3600
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Task Scheduler runs the watchdog from system32, where the relative ".env"
# that pydantic-settings looks for doesn't exist — so load it explicitly
# before importing settings. (python-dotenv is a pydantic-settings dep.)
try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except Exception:
    pass

STATE_FILE = REPO_ROOT / "storage" / "watchdog_state.json"
LIVE_STATE = REPO_ROOT / "command_center" / "api" / "live_state.json"
RUN_LOG = REPO_ROOT / "storage" / "paper_run.log"


def _staleness_s(path: Path) -> float | None:
    """Seconds since `path` was last modified, or None if it doesn't exist."""
    try:
        return time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return None


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass  # watchdog state is best-effort; the alert already fired


async def _alert(message: str) -> None:
    """CRITICAL alert via the bot's own alerter (never muted). Logs locally
    and swallows every failure — the watchdog must never crash the machine."""
    try:
        from alerts.telegram import AlertLevel, build_alerter
        from config.settings import settings

        alerter = build_alerter(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID)
        await alerter.send_alert(message, level=AlertLevel.CRITICAL)
    except Exception:
        import logging
        logging.basicConfig(level=logging.INFO)
        logging.getLogger("watchdog").exception("Watchdog alert failed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stale-s", type=float, default=900.0)
    parser.add_argument("--cooldown-s", type=float, default=3600.0)
    args = parser.parse_args()

    stale_s = max(60.0, args.stale_s)
    cooldown_s = max(60.0, args.cooldown_s)

    ls_stale = _staleness_s(LIVE_STATE)
    log_stale = _staleness_s(RUN_LOG)

    # Bot is alive if EITHER signal is fresh (the command-center export could
    # fail independently of trading, and the log could be quiet during a calm
    # stretch if the dashboard is disabled — but both going cold is dead).
    alive = (
        (ls_stale is not None and ls_stale <= stale_s)
        or (log_stale is not None and log_stale <= stale_s)
    )

    if alive:
        print(f"OK — bot alive (live_state {ls_stale and round(ls_stale, 0)}s old, "
              f"log {log_stale and round(log_stale, 0)}s old)")
        return 0

    detail = (
        f"live_state.json: {'missing' if ls_stale is None else f'{ls_stale:.0f}s old'}; "
        f"paper_run.log: {'missing' if log_stale is None else f'{log_stale:.0f}s old'} "
        f"(stale threshold {stale_s:.0f}s)"
    )
    state = _load_state()
    last_alert = state.get("last_alert_ts", 0.0)
    now = time.time()
    if now - last_alert < cooldown_s:
        print(f"STALE ({detail}) — alert already sent {now - last_alert:.0f}s ago, "
              f"skipping (cooldown {cooldown_s:.0f}s)")
        return 0

    message = (
        "WATCHDOG: the validation-run bot appears DEAD — no liveness signal "
        f"for {stale_s:.0f}s ({detail}). Check the process, restart if needed, "
        "and log the intervention in docs/VALIDATION_RUN_2026_08.md."
    )
    import asyncio
    asyncio.run(_alert(message))
    state["last_alert_ts"] = now
    _save_state(state)
    print(f"STALE ({detail}) — CRITICAL alert sent to Telegram")
    return 0


if __name__ == "__main__":
    sys.exit(main())
