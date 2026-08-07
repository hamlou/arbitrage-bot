"""
Tests for the TelegramAlerter mute behavior (added with the CRM/control
upgrade): mute suppresses routine INFO/WARNING delivery but CRITICAL alerts
always get through, and every path still logs locally.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alerts.telegram import AlertLevel, TelegramAlerter, build_alerter

TOKEN = "123:test-token"
CHAT_ID = "6660139135"


@pytest.mark.asyncio
async def test_muted_alerter_skips_info_and_warning_delivery():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    with patch("telegram.Bot", return_value=bot):
        alerter = TelegramAlerter(TOKEN, CHAT_ID, muted=True)
        await alerter.send_alert("routine", level=AlertLevel.INFO)
        await alerter.send_alert("warning", level=AlertLevel.WARNING)
    assert bot.send_message.await_count == 0  # nothing delivered while muted


@pytest.mark.asyncio
async def test_muted_alerter_still_delivers_critical():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    with patch("telegram.Bot", return_value=bot):
        alerter = TelegramAlerter(TOKEN, CHAT_ID, muted=True)
        await alerter.send_alert("kill switch", level=AlertLevel.CRITICAL)
    assert bot.send_message.await_count == 1  # safety messages always sent


@pytest.mark.asyncio
async def test_unmuted_alerter_delivers_everything():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    with patch("telegram.Bot", return_value=bot):
        alerter = TelegramAlerter(TOKEN, CHAT_ID, muted=False)
        await alerter.send_alert("info", level=AlertLevel.INFO)
        await alerter.send_alert("warn", level=AlertLevel.WARNING)
        await alerter.send_alert("crit", level=AlertLevel.CRITICAL)
    assert bot.send_message.await_count == 3


@pytest.mark.asyncio
async def test_set_muted_toggles_runtime_state():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    with patch("telegram.Bot", return_value=bot):
        alerter = TelegramAlerter(TOKEN, CHAT_ID, muted=False)
        assert alerter.muted is False
        alerter.set_muted(True)
        assert alerter.muted is True
        await alerter.send_alert("silent", level=AlertLevel.INFO)
    assert bot.send_message.await_count == 0


@pytest.mark.asyncio
async def test_build_alerter_passes_muted_default():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    with patch("telegram.Bot", return_value=bot):
        alerter = build_alerter(TOKEN, CHAT_ID, muted=True)
        assert alerter.muted is True


@pytest.mark.asyncio
async def test_muted_alerter_without_credentials_is_disabled():
    alerter = TelegramAlerter(None, None, muted=True)
    assert alerter.enabled is False
    assert await alerter.send_alert("x", level=AlertLevel.CRITICAL) is None  # no raise
