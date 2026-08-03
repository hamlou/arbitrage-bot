"""
Telegram alerting. Designed so that a Telegram outage or missing credentials
NEVER crashes the trading loop — every failure here is caught, logged locally,
and swallowed.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

logger = logging.getLogger(__name__)


class AlertLevel(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


_LEVEL_EMOJI = {
    AlertLevel.INFO: "ℹ️",
    AlertLevel.WARNING: "⚠️",
    AlertLevel.CRITICAL: "🚨",
}


class TelegramAlerter:
    def __init__(self, bot_token: Optional[str], chat_id: Optional[str]):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._bot = None
        if bot_token and chat_id:
            try:
                from telegram import Bot  # imported lazily so the module loads even without the dep configured

                self._bot = Bot(token=bot_token)
            except Exception:
                logger.exception("Failed to initialize Telegram bot; alerts will log locally only")
                self._bot = None
        else:
            logger.info("Telegram not configured (no token/chat id) — alerts will log locally only")

    @property
    def enabled(self) -> bool:
        return self._bot is not None and bool(self.chat_id)

    @retry(
        retry=retry_if_exception_type(Exception),
        wait=wait_exponential_jitter(initial=1, max=10),
        stop=stop_after_attempt(3),
        reraise=False,
    )
    async def _send(self, text: str) -> None:
        await self._bot.send_message(chat_id=self.chat_id, text=text)

    async def send_alert(self, message: str, level: AlertLevel = AlertLevel.INFO) -> None:
        emoji = _LEVEL_EMOJI.get(level, "")
        text = f"{emoji} [{level.value}] {message}"

        # Always log locally regardless of Telegram availability.
        log_fn = {
            AlertLevel.INFO: logger.info,
            AlertLevel.WARNING: logger.warning,
            AlertLevel.CRITICAL: logger.error,
        }[level]
        log_fn("ALERT: %s", message)

        if not self.enabled:
            return

        try:
            await self._send(text)
        except Exception:
            # Alert failures must never propagate into the trading loop.
            logger.exception("Failed to deliver Telegram alert after retries; continuing")


# Convenience singleton-style factory, wired up in main.py from Settings.
def build_alerter(bot_token: Optional[str], chat_id: Optional[str]) -> TelegramAlerter:
    return TelegramAlerter(bot_token=bot_token, chat_id=chat_id)
