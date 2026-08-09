"""
Tests for the Telegram status/reporter layer (alerts/status_report.py and
alerts/telegram.py TelegramReporter) plus TradingApp._build_status_snapshot.

No test touches the network: the telegram Bot and Application are mocked, the
command handlers are exercised with fake Update objects, and the snapshot test
uses the same fake-feed test app as test_main_integration.py.
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alerts.status_report import format_status_report
from alerts.telegram import TelegramReporter, build_reporter
from data.binance_feed import PriceUpdate
from main import TradingApp

TOKEN = "123:test-token"
CHAT_ID = "6660139135"


SNAPSHOT = {
    "mode": "PAPER",
    "balance_usd": 1000.0,
    "equity_usd": 1012.5,
    "total_pnl_usd": 12.5,
    "win_rate_pct": 62.5,
    "closed_trades": 8,
    "open_positions": 1,
    "uptime": "0d 02h 05m",
    "binance_feed_healthy": True,
    "polymarket_feed_healthy": False,
    "daily_halted": False,
    "kill_switch_tripped": False,
    "positions": [
        {"market_id": "m1", "side": "YES", "size_usd": 80.0, "entry_price": 0.53},
    ],
    "recent_trades": [
        {
            "market_id": "m2", "side": "NO",
            "realized_pnl_usd": 5.0, "exit_reason": "SETTLED",
        },
    ],
    "by_strategy": {
        "latency_arb": {"trades": 6, "pnl_usd": 10.0, "win_rate_pct": 66.7},
        "sum_to_one": {"trades": 2, "pnl_usd": 2.5, "win_rate_pct": 50.0},
    },
}


# ---------------------------------------------------------------------------
# format_status_report (pure formatter)
# ---------------------------------------------------------------------------


def test_report_includes_mode_and_account():
    out = format_status_report(SNAPSHOT)
    assert "PAPER (demo)" in out
    assert "Balance" in out and "$1,000.00" in out
    assert "Equity" in out and "$1,012.50" in out
    assert "Total PnL" in out and "+$12.50" in out
    assert "Win rate" in out and "62.5%" in out


def test_report_includes_system_and_feed_health():
    out = format_status_report(SNAPSHOT)
    assert "Binance feed" in out and "OK" in out
    assert "Polymarket feed" in out and "DOWN" in out
    assert "Uptime" in out and "0d 02h 05m" in out


def test_report_includes_positions_recent_and_strategies():
    out = format_status_report(SNAPSHOT)
    assert "OPEN POSITIONS" in out and "m1" in out
    assert "RECENT TRADES" in out and "m2" in out
    assert "BY STRATEGY" in out
    assert "latency_arb" in out and "sum_to_one" in out


def test_report_empty_snapshot_never_raises():
    out = format_status_report({})
    assert "PAPER" not in out  # mode defaults to "?"
    assert "(none)" in out or "n/a" in out


def test_report_live_mode_label():
    out = format_status_report({**SNAPSHOT, "mode": "LIVE"})
    assert "LIVE (real)" in out


def test_report_renders_stats_note_when_present():
    out = format_status_report({**SNAPSHOT, "stats_note": "LIVE trades are on-chain"})
    assert "NOTE: LIVE trades are on-chain" in out


def test_report_omits_note_when_absent():
    out = format_status_report(SNAPSHOT)
    assert "NOTE:" not in out


# ---------------------------------------------------------------------------
# TelegramReporter — send paths (Bot mocked, no network)
# ---------------------------------------------------------------------------


def test_reporter_disabled_without_credentials():
    r = build_reporter(None, None)
    assert r.enabled is False
    assert asyncio.run(r.send_text("hi")) is False
    assert asyncio.run(r.send_status_digest()) is False


@pytest.mark.asyncio
async def test_send_text_success():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    with patch("telegram.Bot", return_value=bot):
        r = TelegramReporter(TOKEN, CHAT_ID)
        ok = await r.send_text("hello")
    assert ok is True
    bot.send_message.assert_awaited_once_with(chat_id=CHAT_ID, text="hello")


@pytest.mark.asyncio
async def test_send_text_swallows_errors():
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))
    with patch("telegram.Bot", return_value=bot):
        r = TelegramReporter(TOKEN, CHAT_ID)
        ok = await r.send_text("hello")
    assert ok is False  # must never raise into the trading loop


@pytest.mark.asyncio
async def test_send_status_digest_formats_snapshot():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    with patch("telegram.Bot", return_value=bot):
        r = TelegramReporter(TOKEN, CHAT_ID, status_provider=lambda: SNAPSHOT)
        ok = await r.send_status_digest()
    assert ok is True
    sent = bot.send_message.await_args.kwargs["text"]
    assert "PAPER (demo)" in sent and "$1,012.50" in sent


@pytest.mark.asyncio
async def test_send_status_digest_swallows_provider_errors():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    with patch("telegram.Bot", return_value=bot):

        def boom():
            raise RuntimeError("db down")

        r = TelegramReporter(TOKEN, CHAT_ID, status_provider=boom)
        ok = await r.send_status_digest()
    assert ok is False


# ---------------------------------------------------------------------------
# Command handlers — chat-gated, fake Update objects
# ---------------------------------------------------------------------------


class FakeChat:
    def __init__(self, chat_id: int):
        self.id = chat_id
        self.sent: list[str] = []
        self.sent_kwargs: list[dict] = []

    async def send_message(self, text: str, **kwargs) -> None:
        self.sent.append(text)
        self.sent_kwargs.append(kwargs)


class FakeUpdate:
    def __init__(self, chat_id: int):
        self.effective_chat = FakeChat(chat_id)


@pytest.mark.asyncio
async def test_cmd_status_ignores_unauthorized_chat():
    r = TelegramReporter(TOKEN, CHAT_ID, status_provider=lambda: SNAPSHOT)
    update = FakeUpdate(12345)  # someone else
    await r._cmd_status(update, None)
    assert update.effective_chat.sent == []


@pytest.mark.asyncio
async def test_cmd_status_answers_configured_chat():
    r = TelegramReporter(TOKEN, CHAT_ID, status_provider=lambda: SNAPSHOT)
    update = FakeUpdate(int(CHAT_ID))
    await r._cmd_status(update, None)
    assert len(update.effective_chat.sent) == 1
    assert "PAPER (demo)" in update.effective_chat.sent[0]


@pytest.mark.asyncio
async def test_cmd_help_gated_and_answers():
    r = TelegramReporter(TOKEN, CHAT_ID)
    stranger = FakeUpdate(1)
    await r._cmd_help(stranger, None)
    assert stranger.effective_chat.sent == []

    owner = FakeUpdate(int(CHAT_ID))
    await r._cmd_help(owner, None)
    assert "/status" in owner.effective_chat.sent[0]


@pytest.mark.asyncio
async def test_cmd_stats_is_alias_of_status():
    r = TelegramReporter(TOKEN, CHAT_ID, status_provider=lambda: SNAPSHOT)
    update = FakeUpdate(int(CHAT_ID))
    await r._cmd_stats(update, None)
    assert "PAPER (demo)" in update.effective_chat.sent[0]


# ---------------------------------------------------------------------------
# run_command_listener — Application mocked
# ---------------------------------------------------------------------------


class FakeUpdater:
    start_polling = AsyncMock()
    stop = AsyncMock()


class FakeApplication:
    def __init__(self):
        self.handlers: list = []
        self.updater = FakeUpdater()
        self.stop_on_poll: asyncio.Event | None = None

    def add_handler(self, h) -> None:
        self.handlers.append(h)

    async def run_polling(self, **kwargs) -> None:
        # Set the stop event once polling actually starts (so the listener's
        # retry loop has a session to run), then block until cancelled —
        # mirrors the real (blocking) run_polling.
        if self.stop_on_poll is not None:
            self.stop_on_poll.set()
        await asyncio.Event().wait()

    async def initialize(self) -> None:
        pass

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


@pytest.mark.asyncio
async def test_run_command_listener_registers_all_commands():
    app = FakeApplication()
    builder = MagicMock()
    builder.token.return_value = builder
    builder.build.return_value = app
    with patch("telegram.ext.Application") as mock_cls:
        mock_cls.builder.return_value = builder  # Application.builder() -> builder
        r = TelegramReporter(TOKEN, CHAT_ID, status_provider=lambda: SNAPSHOT)
        stop = asyncio.Event()
        app.stop_on_poll = stop  # stop once polling starts
        await r.run_command_listener(stop)

    commands = {
        cmd for h in app.handlers if getattr(h, "commands", None) for cmd in h.commands
    }
    assert commands == {
        "start", "menu", "status", "stats", "crm", "positions", "trades",
        "risk", "feeds", "config", "latency", "pause", "resume", "mute",
        "unmute", "alerts", "help",
    }
    # The button interface is registered alongside the slash commands.
    from telegram.ext import CallbackQueryHandler, MessageHandler

    assert any(isinstance(h, CallbackQueryHandler) for h in app.handlers)
    assert any(isinstance(h, MessageHandler) for h in app.handlers)


@pytest.mark.asyncio
async def test_run_command_listener_skips_when_disabled():
    r = TelegramReporter(None, None)
    await r.run_command_listener(asyncio.Event())  # must not raise


@pytest.mark.asyncio
async def test_menu_markup_has_all_buttons():
    """The inline menu must expose every action as a tappable button."""
    r = TelegramReporter(TOKEN, CHAT_ID)
    markup = r._menu_markup()
    data = {btn.callback_data for row in markup.inline_keyboard for btn in row}
    assert data == {
        "btn_status", "btn_positions", "btn_trades", "btn_risk", "btn_feeds",
        "btn_latency", "btn_pause", "btn_resume", "btn_mute", "btn_unmute",
        "btn_help",
    }


@pytest.mark.asyncio
async def test_on_callback_dispatches_and_acknowledges():
    """A button press must be acknowledged and routed to the matching builder."""
    r = TelegramReporter(TOKEN, CHAT_ID, status_provider=lambda: SNAPSHOT)

    class FakeQuery:
        data = "btn_status"
        answered = False

        async def answer(self) -> None:
            self.answered = True

    class FakeChat:
        id = int(CHAT_ID)
        sent: list[tuple] = []

        async def send_message(self, **kwargs) -> None:
            self.sent.append(kwargs)

    class FakeUpdate:
        def __init__(self):
            self.callback_query = FakeQuery()
            self.effective_chat = FakeChat()

    update = FakeUpdate()
    await r._on_callback(update, None)
    assert update.callback_query.answered is True
    assert len(update.effective_chat.sent) == 1  # a status reply was sent


@pytest.mark.asyncio
async def test_run_command_listener_survives_telegram_conflict():
    """A Telegram Conflict (another instance polling the same token) must be
    logged and retried — never propagated, never fatal (this is the failure
    that crashed the cloud instance on 2026-08-09)."""
    r = TelegramReporter(TOKEN, CHAT_ID)
    r.retry_s = 0.01

    calls = {"n": 0}

    async def fake_session(stop):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Exception("Conflict: terminated by other getUpdates request")
        stop.set()  # second attempt succeeds; stop so the loop exits

    r._run_polling_session = fake_session
    stop = asyncio.Event()
    await r.run_command_listener(stop)  # must return without raising
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# TradingApp._build_status_snapshot — fake feeds, no network
# ---------------------------------------------------------------------------


async def test_build_status_snapshot_produces_all_sections(app_settings):
    from tests.test_main_integration import build_app

    app: TradingApp = await build_app(app_settings)
    try:
        now = time.time()
        for i, price in enumerate([65000, 65200, 65400, 65600, 65800, 66000]):
            app.signal_engine.ingest_price_update(
                PriceUpdate(symbol="BTCUSDT", price=price, event_time_ms=0, received_at=now + i, kind="trade")
            )

        from data.polymarket_feed import Market, OrderBook, OrderBookLevel

        def make_market(mid="snap_m"):
            return Market(
                market_id=mid, question="BTC 15m", token_id_yes=f"{mid}_yes", token_id_no=f"{mid}_no",
                liquidity_usd=100_000, end_date_iso="2026-07-31T14:00:00Z", asset="BTC",
                duration_minutes=15, resolved=False, reference_price=65000, expires_at_ts=now + 300,
            )

        def make_book(tok: str, bid: float, ask: float) -> OrderBook:
            size = 300_000 / ((bid + ask) / 2)
            return OrderBook(
                market_id="snap_m", token_id=tok,
                bids=(OrderBookLevel(price=bid, size=size),),
                asks=(OrderBookLevel(price=ask, size=size),),
            )

        market = make_market()
        yes_book = make_book(market.token_id_yes, 0.49, 0.51)
        no_book = make_book(market.token_id_no, 0.49, 0.51)
        app.feed.register(market, yes_book, no_book)
        app._known_markets[market.market_id] = market

        await app.broker.place_order(market, "YES", 100)
        snap = await app._build_status_snapshot()

        assert snap["mode"] == "PAPER"
        assert snap["balance_usd"] < 1000.0  # spent on the position (minus fee)
        assert snap["open_positions"] == 1
        assert snap["positions"][0]["side"] == "YES"
        assert snap["closed_trades"] == 0
        assert snap["uptime"]
        assert snap["binance_feed_healthy"] is False  # no messages recorded yet
        assert "by_strategy" in snap
    finally:
        await app.db.close()


@pytest.fixture
def app_settings(tmp_path):
    from config.settings import Settings

    return Settings(
        _env_file=None,
        DATABASE_PATH=str(tmp_path / "snap.db"),
        MIN_MARKET_LIQUIDITY_USD=1000,
        EDGE_THRESHOLD_PCT=0.03,
        MIN_CONFIDENCE=0.1,
        STARTING_PAPER_BALANCE_USD=1000,
        MAX_TOTAL_EXPOSURE_PCT=0.5,
    )


# ---------------------------------------------------------------------------
# HTML CRM formatters
# ---------------------------------------------------------------------------

from alerts.status_report import (  # noqa: E402
    format_config_html,
    format_crm_html,
    format_feeds_html,
    format_latency_html,
    format_positions_html,
    format_risk_html,
    format_trades_html,
)


def test_crm_html_includes_account_and_system():
    out = format_crm_html(SNAPSHOT)
    assert "PAPER (demo)" in out
    assert "ACCOUNT" in out and "$1,000.00" in out
    assert "SYSTEM" in out
    assert "FEED HEALTH" in out
    assert "OPEN POSITIONS" in out and "m1" in out
    assert "RECENT TRADES" in out and "m2" in out
    assert "BY STRATEGY" in out


def test_crm_html_escapes_user_derived_text():
    snap = {**SNAPSHOT, "positions": [{"market_id": "<evil>", "side": "YES", "size_usd": 1, "entry_price": 0.5}]}
    out = format_crm_html(snap)
    assert "<evil>" not in out
    assert "&lt;evil&gt;" in out


def test_crm_html_never_raises_on_empty_snapshot():
    out = format_crm_html({})
    assert "n/a" in out or "none" in out


def test_section_formatters_render_their_sections():
    assert "OPEN POSITIONS" in format_positions_html(SNAPSHOT)
    assert "RECENT TRADES" in format_trades_html(SNAPSHOT)
    assert "RISK" in format_risk_html({**SNAPSHOT, "risk_detail": {"daily_pnl_pct": -0.02, "drawdown_pct": 0.05}})
    assert "FEED HEALTH" in format_feeds_html(SNAPSHOT)
    assert "CONFIG" in format_config_html({**SNAPSHOT, "config": {"mode": "PAPER"}})
    assert "LATENCY" in format_latency_html({**SNAPSHOT, "latency": {"tick_to_order_p95_ms": 300.0}})


def test_latency_html_shows_verdict():
    snap = {
        **SNAPSHOT,
        "latency": {
            "tick_to_signal_p50_ms": 40.0, "tick_to_signal_p95_ms": 90.0,
            "tick_to_order_p50_ms": 120.0, "tick_to_order_p95_ms": 250.0,
            "platform_delay_ms": 250.0, "window_s": 2.0, "verdict": "comfortable",
        },
    }
    out = format_latency_html(snap)
    assert "comfortable" in out and "250" in out


def test_config_html_lists_keys_alphabetically():
    snap = {**SNAPSHOT, "config": {"mode": "PAPER", "edge_threshold_pct": "5.00%"}}
    out = format_config_html(snap)
    assert "edge_threshold_pct" in out and "5.00%" in out
    assert out.index("CONFIG") < out.index("edge_threshold_pct")


# ---------------------------------------------------------------------------
# send_html + chunking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_html_uses_parse_mode_and_returns_true():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    with patch("telegram.Bot", return_value=bot):
        r = TelegramReporter(TOKEN, CHAT_ID)
        ok = await r.send_html("<b>hi</b>")
    assert ok is True
    bot.send_message.assert_awaited_once_with(chat_id=CHAT_ID, text="<b>hi</b>", parse_mode="HTML")


@pytest.mark.asyncio
async def test_send_html_chunks_long_messages():
    from alerts.telegram import MAX_MESSAGE_CHARS

    bot = MagicMock()
    bot.send_message = AsyncMock()
    with patch("telegram.Bot", return_value=bot):
        r = TelegramReporter(TOKEN, CHAT_ID)
        long_html = "\n".join(f"<b>line {i}</b> x" * 40 for i in range(60))
        ok = await r.send_html(long_html)
    assert ok is True
    assert bot.send_message.await_count > 1  # split into chunks
    for call in bot.send_message.await_args_list:
        assert len(call.kwargs["text"]) <= MAX_MESSAGE_CHARS
        assert call.kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_send_html_swallows_errors():
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))
    with patch("telegram.Bot", return_value=bot):
        r = TelegramReporter(TOKEN, CHAT_ID)
        ok = await r.send_html("<b>hi</b>")
    assert ok is False


def test_chunk_html_hard_splits_overlong_single_line():
    from alerts.telegram import MAX_MESSAGE_CHARS, _chunk_html

    overlong = "x" * (MAX_MESSAGE_CHARS + 100)
    chunks = _chunk_html(overlong, MAX_MESSAGE_CHARS)
    assert len(chunks) >= 2
    assert all(len(c) <= MAX_MESSAGE_CHARS for c in chunks)
    assert "".join(chunks) == overlong  # no content lost


# ---------------------------------------------------------------------------
# Control commands (/pause /resume /mute /unmute /alerts)
# ---------------------------------------------------------------------------


class FakeControls:
    def __init__(self):
        self.paused = False
        self.muted = False

    async def set_paused(self, paused: bool) -> str:
        self.paused = paused
        return "PAUSED" if paused else "RESUMED"

    def set_muted(self, muted: bool) -> str:
        self.muted = muted
        return "MUTED" if muted else "UNMUTED"

    def is_paused(self) -> bool:
        return self.paused

    def is_muted(self) -> bool:
        return self.muted


@pytest.mark.asyncio
async def test_cmd_pause_resume_toggle_controls():
    controls = FakeControls()
    r = TelegramReporter(TOKEN, CHAT_ID, status_provider=lambda: SNAPSHOT, controls=controls)
    owner = FakeUpdate(int(CHAT_ID))

    await r._cmd_pause(owner, None)
    assert controls.paused is True
    assert "PAUSED" in owner.effective_chat.sent[0]

    await r._cmd_resume(owner, None)
    assert controls.paused is False
    assert "RESUMED" in owner.effective_chat.sent[1]


@pytest.mark.asyncio
async def test_cmd_mute_unmute_toggle_controls():
    controls = FakeControls()
    r = TelegramReporter(TOKEN, CHAT_ID, status_provider=lambda: SNAPSHOT, controls=controls)
    owner = FakeUpdate(int(CHAT_ID))

    await r._cmd_mute(owner, None)
    assert controls.muted is True

    await r._cmd_unmute(owner, None)
    assert controls.muted is False


@pytest.mark.asyncio
async def test_control_commands_gated_to_configured_chat():
    controls = FakeControls()
    r = TelegramReporter(TOKEN, CHAT_ID, status_provider=lambda: SNAPSHOT, controls=controls)
    stranger = FakeUpdate(999)
    await r._cmd_pause(stranger, None)
    await r._cmd_mute(stranger, None)
    assert stranger.effective_chat.sent == []
    assert controls.paused is False and controls.muted is False


@pytest.mark.asyncio
async def test_control_commands_answer_when_no_controls_wired():
    r = TelegramReporter(TOKEN, CHAT_ID, status_provider=lambda: SNAPSHOT)
    owner = FakeUpdate(int(CHAT_ID))
    await r._cmd_pause(owner, None)
    assert "not available" in owner.effective_chat.sent[0]


@pytest.mark.asyncio
async def test_cmd_alerts_reports_state():
    controls = FakeControls()
    controls.paused = True
    controls.muted = True
    r = TelegramReporter(TOKEN, CHAT_ID, status_provider=lambda: SNAPSHOT, controls=controls)
    owner = FakeUpdate(int(CHAT_ID))
    await r._cmd_alerts(owner, None)
    assert "PAUSED" in owner.effective_chat.sent[0]
    assert "MUTED" in owner.effective_chat.sent[0]


@pytest.mark.asyncio
async def test_cmd_crm_sends_html_to_configured_chat():
    r = TelegramReporter(TOKEN, CHAT_ID, status_provider=lambda: SNAPSHOT)
    owner = FakeUpdate(int(CHAT_ID))
    await r._cmd_crm(owner, None)
    assert len(owner.effective_chat.sent) == 1
    assert owner.effective_chat.sent_kwargs[0].get("parse_mode") == "HTML"
    assert "ACCOUNT" in owner.effective_chat.sent[0]


# ---------------------------------------------------------------------------
# Dashboard balance refresh (the $0-balance fix)
# ---------------------------------------------------------------------------


async def test_dashboard_balance_shows_real_money_even_when_feeds_unhealthy(app_settings):
    """
    Regression test for the $0-balance bug: the dashboard showed $0.00 when
    the trading cycle was skipped by the feed-health gate (or any early
    return), because balance was only written after the gate. _refresh_dashboard_state
    must populate balance/PnL/positions at the TOP of the cycle, before the gate.
    """
    from tests.test_main_integration import build_app

    app: TradingApp = await build_app(app_settings)
    try:
        # No feed messages -> feeds unhealthy -> cycle would skip.
        await app._trading_cycle()

        assert app._dashboard_state.balance_usd == pytest.approx(1000.0)
        assert app._dashboard_state.binance_feed_healthy is False
    finally:
        await app.db.close()


async def test_dashboard_refresh_populates_pnl_positions_and_trades(app_settings):
    from tests.test_main_integration import build_app, make_book, make_market

    app: TradingApp = await build_app(app_settings)
    try:
        market = make_market(reference_price=65000)
        app.feed.register(
            market,
            make_book(market.token_id_yes, 0.49, 0.51),
            make_book(market.token_id_no, 0.49, 0.51),
        )
        await app.broker.place_order(market, "YES", 100)

        await app._refresh_dashboard_state()
        assert app._dashboard_state.balance_usd < 1000.0
        assert len(app._dashboard_state.open_positions) == 1
        assert app._dashboard_state.open_positions[0]["side"] == "YES"
    finally:
        await app.db.close()


async def test_pause_flag_blocks_new_trades_but_keeps_state(app_settings):
    """The /pause control must stop the trading cycle from opening new
    positions while the account panel still refreshes."""
    from tests.test_main_integration import build_app

    app: TradingApp = await build_app(app_settings)
    try:
        app.feed_health.record_message("binance")
        app.feed_health.record_message("polymarket")
        app._trading_paused = True

        now = time.time()
        for i, price in enumerate([65000, 65200, 65400, 65600, 65800, 66000, 66200, 66400, 66500, 66600]):
            app.signal_engine.ingest_price_update(
                PriceUpdate(symbol="BTCUSDT", price=price, event_time_ms=0, received_at=now + i, kind="trade")
            )
        from tests.test_main_integration import make_book, make_market

        market = make_market(reference_price=65000)
        app.feed.register(
            market,
            make_book(market.token_id_yes, 0.49, 0.51),
            make_book(market.token_id_no, 0.49, 0.51),
        )
        app._known_markets[market.market_id] = market

        await app._trading_cycle()
        assert await app.db.get_open_trades(mode="PAPER") == []  # paused -> no new trade

        app._trading_paused = False
        await app._trading_cycle()
        assert len(await app.db.get_open_trades(mode="PAPER")) == 1  # resumed -> trades
    finally:
        await app.db.close()
