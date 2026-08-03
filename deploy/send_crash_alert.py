"""
systemd ExecStopPost crash-alert hook for polymarket-bot.

Runs automatically by systemd AFTER the main process exits and BEFORE the
unit is restarted (see deploy/polymarket-bot.service). systemd exposes how
the service stopped to ExecStopPost commands through three environment
variables:

    $SERVICE_RESULT   e.g. "success", "exit-code", "signal", "core-dump",
                            "timeout", "watchdog", "start-limit-hit"
    $EXIT_CODE        e.g. "ok", "exited", "killed", "dumped", "timed out"
    $EXIT_STATUS      the numeric exit status (or signal name) if applicable

We alert ONLY on abnormal exits: a clean `systemctl stop` yields
SERVICE_RESULT=success (main.py converts SIGTERM into an orderly shutdown),
and that must stay silent. Anything else — a crash, an OOM kill, a watchdog
timeout, or systemd giving up on a crash loop (start-limit-hit) — means the
bot died on its own and a human should be woken up.

This reuses alerts/telegram.py's build_alerter + TelegramAlerter, so missing
credentials log locally only and this script never raises (the alerter
swallows delivery failures by design).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

# systemd runs this script by absolute path, so the repo root is NOT on
# sys.path automatically — add it so the alerts/ and config/ imports resolve
# regardless of where the unit file points WorkingDirectory.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from alerts.telegram import AlertLevel, build_alerter  # noqa: E402
from config.settings import settings  # noqa: E402


def should_alert(service_result: str) -> bool:
    """True when the service stopped abnormally. systemd reports
    SERVICE_RESULT=success for clean stops (systemctl stop, reboot, manual
    restart); every other value means the process died on its own. An empty
    value means this wasn't invoked by systemd (or it predates the env vars)
    — stay silent rather than page on a false positive."""
    return bool(service_result) and service_result != "success"


async def _send_alert(service_result: str, exit_code: str, exit_status: str) -> None:
    alerter = build_alerter(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID)
    await alerter.send_alert(
        "Bot process exited abnormally — systemd is restarting it "
        f"(service_result={service_result}, exit_code={exit_code}, "
        f"exit_status={exit_status}). Check: journalctl -u polymarket-bot -n 200",
        level=AlertLevel.CRITICAL,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    # Read fresh from the environment each invocation (systemd sets these per
    # ExecStopPost run; reading them here also keeps the hook unit-testable).
    service_result = os.environ.get("SERVICE_RESULT", "")
    if not should_alert(service_result):
        # Clean stop (or not invoked by systemd at all) — nothing to page.
        return 0
    exit_code = os.environ.get("EXIT_CODE", "")
    exit_status = os.environ.get("EXIT_STATUS", "")
    try:
        asyncio.run(_send_alert(service_result, exit_code, exit_status))
    except Exception:
        # The alerter swallows delivery failures internally, but if Telegram
        # itself is unconfigured or the import failed, never block systemd's
        # restart with an unhandled hook error.
        logger = logging.getLogger("send_crash_alert")
        if not logger.handlers:
            logging.basicConfig(level=logging.INFO)
        logger.exception("Crash alert hook failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
