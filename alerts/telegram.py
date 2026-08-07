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
    """
    One-way push alerts (trades, settlements, risk events). The /mute and
    /unmute commands (see TelegramReporter) toggle `muted`, which suppresses
    routine INFO/WARNING delivery — but CRITICAL alerts ALWAYS get through,
    because a mute must never silence a safety message like the kill switch.
    """

    def __init__(self, bot_token: Optional[str], chat_id: Optional[str], muted: bool = False):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.muted = muted
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

    def set_muted(self, muted: bool) -> None:
        self.muted = muted
        logger.info("Telegram alerts %s", "MUTED" if muted else "UNMUTED")

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

        # Mute suppresses routine alerts only. Safety-critical (CRITICAL)
        # alerts always go through — muting must never hide a kill switch.
        if self.muted and level != AlertLevel.CRITICAL:
            logger.info("Telegram alerts muted — %s alert logged locally, not sent", level.value)
            return

        try:
            await self._send(text)
        except Exception:
            # Alert failures must never propagate into the trading loop.
            logger.exception("Failed to deliver Telegram alert after retries; continuing")


MAX_MESSAGE_CHARS = 4000  # Telegram hard-caps messages at 4096; leave headroom.


class TelegramReporter:
    """
    Two-way Telegram channel: pushes the periodic status/digest AND answers
    commands via polling, so you can query the bot on demand instead of only
    receiving alerts. Same failure contract as TelegramAlerter — a Telegram
    outage or a broken handler never crashes the trading loop.

    Commands are gated to the configured chat_id only: anyone else who finds
    the bot's username gets nothing, not even an error message.

    status_provider: an async (or sync) callable returning the snapshot dict
    consumed by alerts.status_report. main.py wires it after startup.

    controls: optional object exposing is_paused() / set_paused(bool) /
    is_muted() / set_muted(bool) so /pause /resume /mute /unmute /alerts can
    actually act on the running app. TradingApp implements exactly this
    interface. If None, control commands answer "not available".
    """

    def __init__(
        self,
        bot_token: Optional[str],
        chat_id: Optional[str],
        status_provider: Optional[object] = None,
        controls: Optional[object] = None,
    ):
        self.bot_token = bot_token
        self.chat_id = str(chat_id) if chat_id is not None else None
        self.status_provider = status_provider
        self.controls = controls
        self._application = None

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    # -- low-level send -----------------------------------------------------

    async def send_text(self, text: str) -> bool:
        """Send raw text; returns True on success, never raises."""
        if not self.enabled:
            return False
        try:
            from telegram import Bot

            bot = Bot(token=self.bot_token)
            await bot.send_message(chat_id=self.chat_id, text=text)
            logger.info("Telegram message sent (%d chars)", len(text))
            return True
        except Exception:
            logger.exception("Failed to send Telegram message; continuing")
            return False

    async def send_html(self, html: str) -> bool:
        """Send an HTML message (parse_mode="HTML"), splitting into chunks if
        it exceeds Telegram's 4096-char cap. Returns True if ALL chunks were
        sent, never raises."""
        if not self.enabled:
            return False
        try:
            from telegram import Bot

            bot = Bot(token=self.bot_token)
            ok = True
            for chunk in _chunk_html(html, MAX_MESSAGE_CHARS):
                await bot.send_message(chat_id=self.chat_id, text=chunk, parse_mode="HTML")
                logger.info("Telegram HTML message sent (%d chars)", len(chunk))
            return ok
        except Exception:
            logger.exception("Failed to send Telegram HTML message; continuing")
            return False

    async def send_status_digest(self) -> bool:
        """Build the current status report from status_provider and send it.
        Never raises: a provider failure (e.g. the DB is briefly busy) degrades
        to a skipped digest, not a crash."""
        if not self.enabled or self.status_provider is None:
            return False
        try:
            from alerts.status_report import format_status_report

            snapshot = await _maybe_await(self.status_provider())
            return await self.send_text(format_status_report(snapshot))
        except Exception:
            logger.exception("Failed to build Telegram status digest; continuing")
            return False

    async def send_crm_digest(self) -> bool:
        """Build and send the full HTML CRM dashboard (the /crm message) as the
        periodic push. Never raises."""
        if not self.enabled or self.status_provider is None:
            return False
        try:
            from alerts.status_report import format_crm_html

            snapshot = await _maybe_await(self.status_provider())
            return await self.send_html(format_crm_html(snapshot))
        except Exception:
            logger.exception("Failed to build Telegram CRM digest; continuing")
            return False

    def _answer(self, text: str, html: bool = False) -> dict:
        kw = {"text": text}
        if html:
            kw["parse_mode"] = "HTML"
        return kw

    # -- on-demand commands (polling) ----------------------------------------

    def _authorized(self, update) -> bool:
        chat = getattr(update, "effective_chat", None)
        return self.chat_id is not None and chat is not None and str(chat.id) == self.chat_id

    async def _send_reply(self, update, text: str, html: bool = False) -> None:
        """Send a reply to the command's chat; swallows all failures."""
        try:
            kwargs = self._answer(text, html=html)
            await update.effective_chat.send_message(**kwargs)
        except Exception:
            logger.exception("Failed to answer Telegram command")

    async def _get_snapshot(self) -> dict:
        if self.status_provider is None:
            return {}
        return (await _maybe_await(self.status_provider())) or {}

    async def _cmd_status(self, update, context) -> None:
        if not self._authorized(update):
            return
        try:
            if self.status_provider is None:
                await self._send_reply(update, "Status unavailable: no data provider wired.")
                return
            from alerts.status_report import format_status_report

            snapshot = await self._get_snapshot()
            await self._send_reply(update, format_status_report(snapshot))
        except Exception:
            logger.exception("Failed to answer /status command")

    async def _cmd_stats(self, update, context) -> None:
        # /stats is an alias of /status — the digest already covers both the
        # account and the per-strategy breakdown.
        await self._cmd_status(update, context)

    async def _cmd_crm(self, update, context) -> None:
        """Full HTML CRM dashboard."""
        if not self._authorized(update):
            return
        try:
            from alerts.status_report import format_crm_html

            snapshot = await self._get_snapshot()
            await self._send_reply(update, format_crm_html(snapshot), html=True)
        except Exception:
            logger.exception("Failed to answer /crm command")

    async def _cmd_positions(self, update, context) -> None:
        if not self._authorized(update):
            return
        try:
            from alerts.status_report import format_positions_html

            snapshot = await self._get_snapshot()
            await self._send_reply(update, format_positions_html(snapshot), html=True)
        except Exception:
            logger.exception("Failed to answer /positions command")

    async def _cmd_trades(self, update, context) -> None:
        if not self._authorized(update):
            return
        try:
            from alerts.status_report import format_trades_html

            snapshot = await self._get_snapshot()
            await self._send_reply(update, format_trades_html(snapshot), html=True)
        except Exception:
            logger.exception("Failed to answer /trades command")

    async def _cmd_risk(self, update, context) -> None:
        if not self._authorized(update):
            return
        try:
            from alerts.status_report import format_risk_html

            snapshot = await self._get_snapshot()
            await self._send_reply(update, format_risk_html(snapshot), html=True)
        except Exception:
            logger.exception("Failed to answer /risk command")

    async def _cmd_feeds(self, update, context) -> None:
        if not self._authorized(update):
            return
        try:
            from alerts.status_report import format_feeds_html

            snapshot = await self._get_snapshot()
            await self._send_reply(update, format_feeds_html(snapshot), html=True)
        except Exception:
            logger.exception("Failed to answer /feeds command")

    async def _cmd_config(self, update, context) -> None:
        if not self._authorized(update):
            return
        try:
            from alerts.status_report import format_config_html

            snapshot = await self._get_snapshot()
            await self._send_reply(update, format_config_html(snapshot), html=True)
        except Exception:
            logger.exception("Failed to answer /config command")

    async def _cmd_latency(self, update, context) -> None:
        if not self._authorized(update):
            return
        try:
            from alerts.status_report import format_latency_html

            snapshot = await self._get_snapshot()
            await self._send_reply(update, format_latency_html(snapshot), html=True)
        except Exception:
            logger.exception("Failed to answer /latency command")

    # -- control commands (need controls wired by main.py) --------------------

    async def _cmd_pause(self, update, context) -> None:
        if not self._authorized(update):
            return
        try:
            if self.controls is None or not hasattr(self.controls, "set_paused"):
                await self._send_reply(update, "Pause control not available — no controls wired.")
                return
            result = self.controls.set_paused(True)
            if hasattr(result, "__await__"):
                result = await result
            await self._send_reply(update, str(result))
        except Exception:
            logger.exception("Failed to answer /pause command")

    async def _cmd_resume(self, update, context) -> None:
        if not self._authorized(update):
            return
        try:
            if self.controls is None or not hasattr(self.controls, "set_paused"):
                await self._send_reply(update, "Pause control not available — no controls wired.")
                return
            result = self.controls.set_paused(False)
            if hasattr(result, "__await__"):
                result = await result
            await self._send_reply(update, str(result))
        except Exception:
            logger.exception("Failed to answer /resume command")

    async def _cmd_mute(self, update, context) -> None:
        if not self._authorized(update):
            return
        try:
            if self.controls is None or not hasattr(self.controls, "set_muted"):
                await self._send_reply(update, "Mute control not available — no controls wired.")
                return
            result = self.controls.set_muted(True)
            if hasattr(result, "__await__"):
                result = await result
            await self._send_reply(update, str(result))
        except Exception:
            logger.exception("Failed to answer /mute command")

    async def _cmd_unmute(self, update, context) -> None:
        if not self._authorized(update):
            return
        try:
            if self.controls is None or not hasattr(self.controls, "set_muted"):
                await self._send_reply(update, "Mute control not available — no controls wired.")
                return
            result = self.controls.set_muted(False)
            if hasattr(result, "__await__"):
                result = await result
            await self._send_reply(update, str(result))
        except Exception:
            logger.exception("Failed to answer /unmute command")

    async def _cmd_alerts(self, update, context) -> None:
        """Show current paused/muted state."""
        if not self._authorized(update):
            return
        try:
            paused = False
            muted = False
            if self.controls is not None:
                paused = bool(getattr(self.controls, "is_paused", lambda: False)())
                muted = bool(getattr(self.controls, "is_muted", lambda: False)())
            text = (
                f"Trading: {'PAUSED' if paused else 'active'}\n"
                f"Alerts: {'MUTED' if muted else 'on'}\n\n"
                "Commands: /pause /resume /mute /unmute"
            )
            await self._send_reply(update, text)
        except Exception:
            logger.exception("Failed to answer /alerts command")

    async def _cmd_help(self, update, context) -> None:
        if not self._authorized(update):
            return
        try:
            interval_h = 6.0
            try:
                from config.settings import settings as _settings

                interval_h = _settings.TELEGRAM_STATUS_INTERVAL_HOURS
            except Exception:
                pass
            text = (
                "<b>Polymarket Arb Bot commands</b>\n\n"
                "<b>Status</b>\n"
                "/status — full plain-text status & stats\n"
                "/stats — alias of /status\n"
                "/crm — full dashboard (HTML)\n"
                "/positions — open positions (HTML)\n"
                "/trades — recent trades (HTML)\n"
                "/risk — risk manager state (HTML)\n"
                "/feeds — feed health detail (HTML)\n"
                "/config — key settings (HTML)\n"
                "/latency — timing vs arbitrage window (HTML)\n\n"
                "<b>Control</b>\n"
                "/pause — stop opening new trades (positions still managed)\n"
                "/resume — resume opening new trades\n"
                "/mute — mute routine alerts (CRITICAL always delivered)\n"
                "/unmute — unmute alerts\n"
                "/alerts — show paused/muted state\n\n"
                f"<i>The bot also pushes a CRM digest automatically every {interval_h:.0f} hours.</i>"
            )
            await self._send_reply(update, text, html=True)
        except Exception:
            logger.exception("Failed to answer /help command")

    async def run_command_listener(self, stop_event) -> None:
        """
        Start PTB polling for commands inside the app's existing asyncio loop
        and block until stop_event is set. All command failures are caught
        inside the handlers; this only raises if polling itself can't start.
        """
        if not self.enabled:
            return
        try:
            from telegram.ext import Application, CommandHandler
        except Exception:
            logger.exception("python-telegram-bot Application unavailable; commands disabled")
            return

        application = Application.builder().token(self.bot_token).build()
        self._application = application
        for name, handler in [
            ("status", self._cmd_status),
            ("stats", self._cmd_stats),
            ("crm", self._cmd_crm),
            ("positions", self._cmd_positions),
            ("trades", self._cmd_trades),
            ("risk", self._cmd_risk),
            ("feeds", self._cmd_feeds),
            ("config", self._cmd_config),
            ("latency", self._cmd_latency),
            ("pause", self._cmd_pause),
            ("resume", self._cmd_resume),
            ("mute", self._cmd_mute),
            ("unmute", self._cmd_unmute),
            ("alerts", self._cmd_alerts),
            ("help", self._cmd_help),
        ]:
            application.add_handler(CommandHandler(name, handler))

        # Whole body wrapped so a failure mid-start still shuts the partially
        # initialized Application down (a leaked PTB Application holds an open
        # httpx client + polling tasks).
        updater = None
        try:
            await application.initialize()
            await application.start()
            updater = getattr(application, "updater", None)
            if updater is not None:
                await updater.start_polling(drop_pending_updates=True)
            await stop_event.wait()
        finally:
            for step in (
                lambda: updater.stop() if updater is not None else None,
                application.stop,
                application.shutdown,
            ):
                try:
                    result = step()
                    if result is not None and hasattr(result, "__await__"):
                        await result
                except Exception:
                    logger.exception("Error during Telegram application cleanup; continuing")

    async def stop(self) -> None:
        """Graceful stop for tests / shutdown paths that don't use stop_event."""
        if self._application is not None:
            try:
                await self._application.stop()
                await self._application.shutdown()
            except Exception:
                logger.exception("Error stopping Telegram application")
            self._application = None


async def _maybe_await(value) -> object:
    """Await if the provider returned a coroutine; otherwise pass through
    (lets tests use a plain dict provider without async ceremony)."""
    if hasattr(value, "__await__"):
        return await value
    return value


def _chunk_html(html: str, max_chars: int = MAX_MESSAGE_CHARS) -> list[str]:
    """
    Split an HTML message into <= max_chars chunks on newline boundaries, so
    a long CRM dashboard never trips Telegram's 4096-char cap. Chunking only
    at newlines keeps every chunk a complete line (and thus well-formed
    HTML, since each line carries its own tags).
    """
    if len(html) <= max_chars:
        return [html]
    chunks: list[str] = []
    current = ""
    for line in html.split("\n"):
        if current and len(current) + len(line) + 1 > max_chars:
            chunks.append(current)
            current = ""
        if len(line) > max_chars:
            # A single pathological line longer than the cap (e.g. a huge
            # unbroken market id): hard-split it rather than sending an
            # over-limit message Telegram would reject.
            for i in range(0, len(line), max_chars):
                chunks.append(line[i : i + max_chars])
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


# Convenience singleton-style factory, wired up in main.py from Settings.
def build_alerter(bot_token: Optional[str], chat_id: Optional[str], muted: bool = False) -> TelegramAlerter:
    return TelegramAlerter(bot_token=bot_token, chat_id=chat_id, muted=muted)


def build_reporter(
    bot_token: Optional[str],
    chat_id: Optional[str],
    status_provider: Optional[object] = None,
    controls: Optional[object] = None,
) -> TelegramReporter:
    return TelegramReporter(
        bot_token=bot_token, chat_id=chat_id, status_provider=status_provider, controls=controls,
    )
