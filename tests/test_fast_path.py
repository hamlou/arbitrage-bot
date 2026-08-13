"""
Tests for the event-driven fast path (win-the-gap). Two layers:

1. fast_path_should_run() — the pure trigger decision (cooldown + cumulative
   move threshold), fully unit-tested.
2. End-to-end: a meaningful Binance move triggers an immediate evaluation
   against the WS-cached books (no poll, no REST) and opens a position;
   small/noisy moves correctly do NOT trigger.
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from config.settings import Settings
from data.binance_feed import PriceUpdate
from data.polymarket_feed import Market, OrderBook, OrderBookLevel
from main import TradingApp, fast_path_should_run
import config.settings as settings_module


def make_market(market_id="m1", reference_price=65000, expires_in_s=300) -> Market:
    return Market(
        market_id=market_id, question="Bitcoin Up or Down - 15 min",
        token_id_yes=f"{market_id}_yes", token_id_no=f"{market_id}_no",
        liquidity_usd=100_000, end_date_iso="2026-07-31T14:00:00Z",
        asset="BTC", duration_minutes=15, resolved=False,
        reference_price=reference_price, expires_at_ts=time.time() + expires_in_s,
    )


def make_book(token_id: str, best_bid: float, best_ask: float, depth=300_000) -> OrderBook:
    size = depth / ((best_bid + best_ask) / 2)
    return OrderBook(
        market_id="m1", token_id=token_id,
        bids=(OrderBookLevel(price=best_bid, size=size),),
        asks=(OrderBookLevel(price=best_ask, size=size),),
    )


@pytest.fixture
def app_settings(tmp_path):
    return Settings(
        _env_file=None,
        DATABASE_PATH=str(tmp_path / "fastpath.db"),
        MIN_MARKET_LIQUIDITY_USD=1000,
        EDGE_THRESHOLD_PCT=0.03,
        MIN_CONFIDENCE=0.1,
        STARTING_PAPER_BALANCE_USD=1000,
        MAX_TOTAL_EXPOSURE_PCT=0.5,
        FAST_PATH_MOVE_TRIGGER_PCT=0.001,
        FAST_PATH_COOLDOWN_S=0.5,
    )


async def build_app(app_settings) -> TradingApp:
    # Point the module-level settings singleton at our test settings (main.py
    # reads the module-level `settings`).
    for field in type(app_settings).model_fields:
        setattr(settings_module.settings, field, getattr(app_settings, field))
    app = TradingApp()

    class FakePolymarketFeed:
        def __init__(self):
            self.books: dict[str, OrderBook] = {}
            self._by_id: dict[str, Market] = {}

        async def discover_active_markets(self):
            return list(self._by_id.values())

        async def get_order_book(self, market_id: str, token_id: str) -> OrderBook:
            return self.books[token_id]

        async def get_market_outcome(self, market_id: str):
            return None

        async def get_market_by_id(self, market_id: str):
            return self._by_id.get(market_id)

        async def aclose(self):
            pass

        def register(self, market: Market, yes_book: OrderBook, no_book: OrderBook):
            self._by_id[market.market_id] = market
            self.books[market.token_id_yes] = yes_book
            self.books[market.token_id_no] = no_book

    app.feed = FakePolymarketFeed()
    await app.setup()
    return app


def _warm_tracker(app: TradingApp, price: float, n: int = 200, now: float | None = None) -> float:
    """Feed n stable ticks so the volatility estimate is dominated by the
    real (stable) series, not by the single jump we add later — mirroring a
    live feed with hundreds of ticks in the 120s vol window."""
    now = now or time.time()
    for i in range(n):
        app.signal_engine.ingest_price_update(
            PriceUpdate(symbol="BTCUSDT", price=price, event_time_ms=0, received_at=now + i * 0.1, kind="trade")
        )
    return now


def _seed_ws_cache(app: TradingApp, market: Market, yes_book: OrderBook, no_book: OrderBook) -> None:
    app.ws_feed._books[market.token_id_yes] = yes_book
    app.ws_feed._books[market.token_id_no] = no_book
    app.ws_feed._last_update_at[market.token_id_yes] = time.time()
    app.ws_feed._last_update_at[market.token_id_no] = time.time()


# -- Pure trigger logic -------------------------------------------------------


def test_trigger_runs_on_first_sight():
    assert fast_path_should_run(None, 65000.0, None, 100.0, 0.001, 0.5) is True


def test_trigger_skips_within_cooldown():
    assert fast_path_should_run(65000.0, 65010.0, 100.0, 100.2, 0.001, 0.5) is False


def test_trigger_skips_small_move_after_cooldown():
    # 65008 vs 65000 = 0.012% — well under the 0.10% trigger.
    assert fast_path_should_run(65000.0, 65008.0, 100.0, 100.8, 0.001, 0.5) is False


def test_trigger_runs_on_big_move_after_cooldown():
    # 65070 vs 65000 = 0.108% — clears the 0.10% trigger.
    assert fast_path_should_run(65000.0, 65070.0, 100.0, 100.8, 0.001, 0.5) is True


def test_trigger_is_cumulative_not_tick_to_tick():
    """A move accumulated over several ticks must count — the trigger compares
    against the last EVALUATED price, not the previous tick."""
    # 65000 -> 65010 -> 65035: each tick is < 0.10% of its predecessor, but
    # 65035 vs the last evaluated 65000 is 0.054%... still below; use a series
    # where the cumulative crosses the threshold.
    assert fast_path_should_run(65000.0, 65066.0, 100.0, 101.2, 0.001, 0.5) is True


# -- End-to-end fast path -----------------------------------------------------


async def test_small_moves_do_not_trigger_the_fast_path(app_settings):
    app = await build_app(app_settings)
    try:
        app.feed_health.record_message("binance")
        app.feed_health.record_message("polymarket")

        now = _warm_tracker(app, 65000.0)
        market = make_market(reference_price=65000)
        yes_book = make_book(market.token_id_yes, 0.49, 0.51)
        no_book = make_book(market.token_id_no, 0.49, 0.51)
        app.feed.register(market, yes_book, no_book)
        app._known_markets[market.market_id] = market
        _seed_ws_cache(app, market, yes_book, no_book)

        # First tick arms the path (no prior state) and evaluates: price ==
        # reference, book at 50/50 -> no edge -> no trade.
        await app._maybe_fast_path(PriceUpdate(symbol="BTCUSDT", price=65000.0, event_time_ms=0, received_at=now + 20.0, kind="trade"))
        # Within cooldown -> no trigger.
        await app._maybe_fast_path(PriceUpdate(symbol="BTCUSDT", price=65001.0, event_time_ms=0, received_at=now + 20.1, kind="trade"))
        # Past cooldown but tiny move (0.015%) -> no trigger.
        await app._maybe_fast_path(PriceUpdate(symbol="BTCUSDT", price=65010.0, event_time_ms=0, received_at=now + 20.6, kind="trade"))

        assert await app.db.get_open_trades(mode="PAPER") == []
    finally:
        await app.db.close()


async def test_big_move_triggers_fast_path_and_opens_position(app_settings):
    app = await build_app(app_settings)
    try:
        app.feed_health.record_message("binance")
        app.feed_health.record_message("polymarket")

        now = _warm_tracker(app, 65000.0)
        market = make_market(reference_price=65000)
        # Arm-phase books at ~50/50: with price == reference there is no edge,
        # so the no-op ticks below genuinely don't trade. (The jump phase
        # swaps in cheap books — see below.)
        yes_book = make_book(market.token_id_yes, 0.49, 0.51)
        no_book = make_book(market.token_id_no, 0.49, 0.51)
        app.feed.register(market, yes_book, no_book)
        app._known_markets[market.market_id] = market
        _seed_ws_cache(app, market, yes_book, no_book)

        # No-op ticks first (arm the state without trading). In production the
        # ingest loop calls ingest_price_update BEFORE the trigger — mirror
        # that here.
        for px, ts in [(65000.0, now + 20.0), (65005.0, now + 20.2)]:
            tick = PriceUpdate(symbol="BTCUSDT", price=px, event_time_ms=0, received_at=ts, kind="trade")
            app.signal_engine.ingest_price_update(tick, source="binance")
            await app._maybe_fast_path(tick)

        # The jump finds Polymarket still stale: swap the books to one where
        # the YES ask sits below the 0.45 MAX_DIRECTIONAL_ENTRY_PRICE default
        # (entry allowed) and NO is priced high (pair sums >= $1, so no
        # sum-to-one interferes) — this test is about the fast path, not the cap.
        cheap_yes = make_book(market.token_id_yes, 0.39, 0.41)
        dear_no = make_book(market.token_id_no, 0.59, 0.61)
        app.feed.books[market.token_id_yes] = cheap_yes
        app.feed.books[market.token_id_no] = dear_no
        app.ws_feed._books[market.token_id_yes] = cheap_yes
        app.ws_feed._books[market.token_id_no] = dear_no
        app.ws_feed._last_update_at[market.token_id_yes] = time.time()
        app.ws_feed._last_update_at[market.token_id_no] = time.time()

        # A real move: +1.5% in a ~1.5s burst — price is now well above the
        # reference while Polymarket still shows ~50/50. The fast path must
        # evaluate IMMEDIATELY (not wait for the 1s poll) and buy YES.
        jump = PriceUpdate(symbol="BTCUSDT", price=66000.0, event_time_ms=0, received_at=now + 21.5, kind="trade")
        app.signal_engine.ingest_price_update(jump, source="binance")
        await app._maybe_fast_path(jump)
        # The worker runs as a task (so it can never stall the ingest loop) —
        # wait for it before asserting.
        await asyncio.wait_for(app._fast_path_task, timeout=10)

        open_trades = await app.db.get_open_trades(mode="PAPER")
        assert len(open_trades) == 1
        assert open_trades[0]["side"] == "YES"

        # The latency event for this entry must be measured from the TRUE tick
        # time (the jump), not from when the cycle happened to run.
        events = await app.db.get_latency_events()
        fired = [e for e in events if e.get("fired")]
        assert fired, "fast-path entry should have recorded a fired latency event"
        assert all(e["tick_received_at"] >= now + 21.0 for e in fired)
    finally:
        await app.db.close()


async def test_live_mode_fast_path_never_passes_book_source(app_settings):
    """
    Regression guard (reviewed 2026-08-07): the WS book source is a
    PAPER-mode optimization. LiveBroker.place_order(market, side, size_usd)
    takes no book_source kwarg, so the fast path must NOT pass it in live
    mode — doing so TypeErrors on every live fast-path order (swallowed, and
    the order silently never happens).
    """
    app = await build_app(app_settings)
    try:
        app.feed_health.record_message("binance")
        app.feed_health.record_message("polymarket")

        now = _warm_tracker(app, 65000.0)
        market = make_market(reference_price=65000)
        # Books below the 0.45 MAX_DIRECTIONAL_ENTRY_PRICE default so the fast
        # path's entry is allowed (this test is about the live/paper kwarg, not
        # the cap).
        yes_book = make_book(market.token_id_yes, 0.39, 0.41)
        no_book = make_book(market.token_id_no, 0.39, 0.41)
        app.feed.register(market, yes_book, no_book)
        app._known_markets[market.market_id] = market
        _seed_ws_cache(app, market, yes_book, no_book)

        # A fake LIVE broker (real LiveBroker is mocked out; we only care that
        # place_order is called without the paper-only kwarg).
        broker = MagicMock()
        broker.mode = "LIVE"
        broker.min_order_size_usd = 0.0
        broker.get_balance = AsyncMock(return_value=1000.0)
        broker.place_order = AsyncMock(return_value=MagicMock(avg_price=0.55))
        app.broker = broker

        jump = PriceUpdate(symbol="BTCUSDT", price=66000.0, event_time_ms=0, received_at=now + 21.5, kind="trade")
        app.signal_engine.ingest_price_update(jump, source="binance")
        await app._maybe_fast_path(jump)
        await asyncio.wait_for(app._fast_path_task, timeout=10)

        assert broker.place_order.called
        _, kwargs = broker.place_order.call_args
        assert "book_source" not in kwargs, "live broker must never receive the paper-only book_source kwarg"
    finally:
        await app.db.close()
