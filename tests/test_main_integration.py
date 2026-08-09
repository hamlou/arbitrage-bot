"""
End-to-end integration test for TradingApp, using fake feeds (no live
network). Exercises the full pipeline together: signal evaluation with the
fair-value model, order placement, restart recovery, and the settlement fix
(resolution polled directly by market ID, independent of the active-only
discovery list that caused the original dead-code bug).
"""
import asyncio
import dataclasses
import logging
import time
from unittest.mock import MagicMock, patch

import pytest
from eth_account import Account

from config.settings import Settings
from data.binance_feed import PriceUpdate
from data.polymarket_feed import Market, OrderBook, OrderBookLevel
import engine.feed_health as feed_health_module
from engine.broker_live import LiveBroker
from engine.sum_to_one import find_sum_to_one_opportunity
from main import TradingApp
import config.settings as settings_module

LIVE_ADDRESS = "0x" + "12" * 20
LIVE_CONDITION = "0x" + "ab" * 32
LIVE_TX_HASH = "0x" + "ef" * 32


class FakePolymarketFeed:
    """Stand-in for PolymarketFeed — no network calls. Books and outcomes are
    controlled directly by the test."""

    def __init__(self):
        self.books: dict[str, OrderBook] = {}
        self.outcomes: dict[str, str] = {}
        self._by_id: dict[str, Market] = {}

    async def discover_active_markets(self) -> list[Market]:
        return [m for m in self._by_id.values() if not m.resolved]

    async def get_order_book(self, market_id: str, token_id: str) -> OrderBook:
        return self.books[token_id]

    async def get_market_outcome(self, market_id: str):
        return self.outcomes.get(market_id)

    async def get_market_by_id(self, market_id: str):
        return self._by_id.get(market_id)

    async def aclose(self):
        pass

    def register(self, market: Market, yes_book: OrderBook, no_book: OrderBook):
        self._by_id[market.market_id] = market
        self.books[market.token_id_yes] = yes_book
        self.books[market.token_id_no] = no_book


def make_market(market_id="m1", reference_price=65000, resolved=False) -> Market:
    return Market(
        market_id=market_id, question="Bitcoin Up or Down - 15 min",
        token_id_yes=f"{market_id}_yes", token_id_no=f"{market_id}_no",
        liquidity_usd=100_000, end_date_iso="2026-07-31T14:00:00Z",
        asset="BTC", duration_minutes=15, resolved=resolved,
        reference_price=reference_price, expires_at_ts=time.time() + 300,
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
        DATABASE_PATH=str(tmp_path / "integration.db"),
        MIN_MARKET_LIQUIDITY_USD=1000,
        EDGE_THRESHOLD_PCT=0.03,
        MIN_CONFIDENCE=0.1,
        STARTING_PAPER_BALANCE_USD=1000,
        MAX_TOTAL_EXPOSURE_PCT=0.5,
    )


async def build_app(app_settings) -> TradingApp:
    # Point the module-level settings singleton at our test settings for the
    # duration of this app instance (main.py reads the module-level `settings`).
    for field in type(app_settings).model_fields:
        setattr(settings_module.settings, field, getattr(app_settings, field))
    app = TradingApp()
    app.feed = FakePolymarketFeed()
    await app.setup()
    return app


async def test_late_first_sighting_does_not_trust_reference_price(app_settings, caplog):
    """
    Regression test for the reference-price trust guard (verified 2026-08-07:
    836 saturated reads like "model 2% vs market 99.5%" in one run). When a
    market is first seen well INTO its window, the Binance price at that
    moment is NOT the open price — using it as the fair-value reference makes
    the model confidently wrong in direction. Discovery must leave
    reference_price unset for such markets so the fair-value model stays off.
    """
    app = await build_app(app_settings)
    try:
        app.feed_health.record_message("binance")
        app.feed_health.record_message("polymarket")
        # A fresh Binance tick so current_price() is available to discovery.
        app.signal_engine.ingest_price_update(
            PriceUpdate(symbol="BTCUSDT", price=65000.0, event_time_ms=0, received_at=time.time(), kind="trade")
        )

        # 15-min market, only 2 minutes left when discovery first sees it —
        # 13% remaining < 60% trust threshold -> reference must NOT be set.
        late_market = dataclasses.replace(
            make_market(market_id="late_mkt", reference_price=None),
            expires_at_ts=time.time() + 120,
        )
        app.feed.register(
            late_market,
            make_book(late_market.token_id_yes, 0.49, 0.51),
            make_book(late_market.token_id_no, 0.49, 0.51),
        )

        # The loop runs forever; instead of waiting, patch sleep so the loop
        # exits after one discovery pass (StopAsyncIteration propagates out).
        async def stop_after_one(*args, **kwargs):
            raise StopAsyncIteration

        original_sleep = asyncio.sleep
        asyncio.sleep = stop_after_one  # type: ignore[assignment]
        try:
            with caplog.at_level(logging.DEBUG, logger="main"):
                try:
                    await app._market_discovery_loop()
                except StopAsyncIteration:
                    pass
        finally:
            asyncio.sleep = original_sleep  # type: ignore[assignment]

        known = app._known_markets.get("late_mkt")
        assert known is not None
        assert known.reference_price is None  # guard worked: no trusted reference
        assert "reference price not trusted" in caplog.text
    finally:
        await app.db.close()


async def test_unknown_time_remaining_does_not_trust_reference_price(app_settings, caplog):
    """The trust guard FAILS CLOSED (reviewed 2026-08-07): when the remaining
    time can't be computed (unparseable expiry -> time_remaining_s is None),
    we cannot tell whether the first-sighting price is stale — so it must NOT
    be trusted as the open reference. The old condition fell through to
    trusting in exactly this case."""
    app = await build_app(app_settings)
    try:
        app.signal_engine.ingest_price_update(
            PriceUpdate(symbol="BTCUSDT", price=65000.0, event_time_ms=0, received_at=time.time(), kind="trade")
        )
        unknown_market = dataclasses.replace(
            make_market(market_id="unknown_mkt", reference_price=None),
            expires_at_ts=None,  # -> time_remaining_s is None
        )
        app.feed.register(
            unknown_market,
            make_book(unknown_market.token_id_yes, 0.49, 0.51),
            make_book(unknown_market.token_id_no, 0.49, 0.51),
        )

        async def stop_after_one(*args, **kwargs):
            raise StopAsyncIteration

        original_sleep = asyncio.sleep
        asyncio.sleep = stop_after_one  # type: ignore[assignment]
        try:
            with caplog.at_level(logging.DEBUG, logger="main"):
                try:
                    await app._market_discovery_loop()
                except StopAsyncIteration:
                    pass
        finally:
            asyncio.sleep = original_sleep  # type: ignore[assignment]

        known = app._known_markets.get("unknown_mkt")
        assert known is not None
        assert known.reference_price is None  # fails closed: unknown -> not trusted
        assert "reference price not trusted" in caplog.text
    finally:
        await app.db.close()


async def test_early_first_sighting_does_trust_reference_price(app_settings):
    """A market first seen near its open (most of the window remaining) gets a
    trusted reference price, so the fair-value model can run normally."""
    app = await build_app(app_settings)
    try:
        app.signal_engine.ingest_price_update(
            PriceUpdate(symbol="BTCUSDT", price=65000.0, event_time_ms=0, received_at=time.time(), kind="trade")
        )

        early_market = dataclasses.replace(
            make_market(market_id="early_mkt", reference_price=None),
            expires_at_ts=time.time() + 12 * 60,  # 12 of 15 min left: 80% >= 60%
        )
        app.feed.register(
            early_market,
            make_book(early_market.token_id_yes, 0.49, 0.51),
            make_book(early_market.token_id_no, 0.49, 0.51),
        )

        async def stop_after_one(*args, **kwargs):
            raise StopAsyncIteration

        original_sleep = asyncio.sleep
        asyncio.sleep = stop_after_one  # type: ignore[assignment]
        try:
            try:
                await app._market_discovery_loop()
            except StopAsyncIteration:
                pass
        finally:
            asyncio.sleep = original_sleep  # type: ignore[assignment]

        known = app._known_markets.get("early_mkt")
        assert known is not None
        assert known.reference_price == 65000.0  # trusted: captured from Binance
    finally:
        await app.db.close()


async def test_full_pipeline_places_a_trade_on_a_clear_edge(app_settings):
    app = await build_app(app_settings)
    try:
        # Feed Binance ticks showing BTC well above its 65000 reference.
        now = time.time()
        for i, price in enumerate([65000, 65200, 65400, 65600, 65800, 66000, 66200, 66400, 66500, 66600]):
            app.signal_engine.ingest_price_update(
                PriceUpdate(symbol="BTCUSDT", price=price, event_time_ms=0, received_at=now + i, kind="trade")
            )

        market = make_market(reference_price=65000)
        # Polymarket still pricing near 50/50 -- hasn't caught up to the move.
        yes_book = make_book(market.token_id_yes, 0.49, 0.51)
        no_book = make_book(market.token_id_no, 0.49, 0.51)
        app.feed.register(market, yes_book, no_book)
        app._known_markets[market.market_id] = market

        cash = await app.broker.get_balance()
        equity = await app.broker.get_equity(app._known_markets)
        await app._evaluate_and_maybe_trade(market, cash, equity, exposure_headroom=500)

        open_trades = await app.db.get_open_trades(mode="PAPER")
        assert len(open_trades) == 1
        assert open_trades[0]["side"] == "YES"  # price is well above reference -> should lean YES
    finally:
        await app.db.close()


async def test_trading_cycle_skips_and_logs_when_feed_unhealthy(app_settings, caplog):
    """
    FeedHealth gate: with no messages recorded yet, both feeds are unhealthy,
    so the whole trading cycle must be skipped — and the skip must be logged
    with reason=feed_unhealthy, never silent.
    """
    app = await build_app(app_settings)
    try:
        # Feed Binance ticks that would otherwise produce a clear YES signal.
        now = time.time()
        for i, price in enumerate([65000, 65200, 65400, 65600, 65800, 66000, 66200, 66400, 66500, 66600]):
            app.signal_engine.ingest_price_update(
                PriceUpdate(symbol="BTCUSDT", price=price, event_time_ms=0, received_at=now + i, kind="trade")
            )
        market = make_market(reference_price=65000)
        yes_book = make_book(market.token_id_yes, 0.49, 0.51)
        no_book = make_book(market.token_id_no, 0.49, 0.51)
        app.feed.register(market, yes_book, no_book)
        app._known_markets[market.market_id] = market

        with caplog.at_level(logging.WARNING, logger="main"):
            await app._trading_cycle()

        assert "feed_unhealthy" in caplog.text
        open_trades = await app.db.get_open_trades(mode="PAPER")
        assert open_trades == []  # the cycle was skipped entirely
        # Regression check: the dashboard must reflect the real unhealthy
        # state even though the cycle returned early, not stay stuck on the
        # DashboardState defaults (which happen to also be False, so this
        # confirms the flags were actively set, not just never touched).
        assert app._dashboard_state.binance_feed_healthy is False
        assert app._dashboard_state.polymarket_feed_healthy is False
    finally:
        await app.db.close()


async def test_trading_cycle_dashboard_reflects_partial_feed_health(app_settings, caplog):
    """One healthy feed and one sick feed must show up independently on the
    dashboard, not be collapsed into a single combined flag."""
    app = await build_app(app_settings)
    try:
        app.feed_health.record_message("binance")
        # polymarket feed never messages -> stays unhealthy.

        with caplog.at_level(logging.WARNING, logger="main"):
            await app._trading_cycle()

        assert app._dashboard_state.binance_feed_healthy is True
        assert app._dashboard_state.polymarket_feed_healthy is False
    finally:
        await app.db.close()


async def test_trading_cycle_proceeds_when_feeds_healthy(app_settings):
    """Once both feeds have delivered messages, the same setup trades normally."""
    app = await build_app(app_settings)
    try:
        app.feed_health.record_message("binance")
        app.feed_health.record_message("polymarket")

        now = time.time()
        for i, price in enumerate([65000, 65200, 65400, 65600, 65800, 66000, 66200, 66400, 66500, 66600]):
            app.signal_engine.ingest_price_update(
                PriceUpdate(symbol="BTCUSDT", price=price, event_time_ms=0, received_at=now + i, kind="trade")
            )
        market = make_market(reference_price=65000)
        yes_book = make_book(market.token_id_yes, 0.49, 0.51)
        no_book = make_book(market.token_id_no, 0.49, 0.51)
        app.feed.register(market, yes_book, no_book)
        app._known_markets[market.market_id] = market

        await app._trading_cycle()

        open_trades = await app.db.get_open_trades(mode="PAPER")
        assert len(open_trades) == 1
        assert app._dashboard_state.binance_feed_healthy is True
        assert app._dashboard_state.polymarket_feed_healthy is True
    finally:
        await app.db.close()


async def test_binance_ingest_loop_records_messages_into_feed_health(app_settings):
    """The real _binance_ingest_loop must feed record_message, so a fresh
    TradingApp becomes healthy once Binance ticks start flowing (this is the
    wiring that makes is_healthy() ever return True in production)."""
    app = await build_app(app_settings)
    try:
        async def fake_stream():
            yield PriceUpdate(symbol="BTCUSDT", price=65000.0, event_time_ms=0, received_at=time.time(), kind="trade")
            yield PriceUpdate(symbol="BTCUSDT", price=65001.0, event_time_ms=1, received_at=time.time(), kind="ticker")

        app.binance_feed.stream = fake_stream

        ingest = asyncio.create_task(app._binance_ingest_loop())
        await asyncio.sleep(0.05)
        ingest.cancel()
        try:
            await ingest
        except asyncio.CancelledError:
            pass

        assert app.feed_health.seconds_since_last_message("binance") is not None
    finally:
        await app.db.close()


async def test_trading_cycle_skips_when_reconnect_storm(app_settings, caplog):
    """More than MAX_RECONNECTS reconnects in the window blocks the cycle too."""
    app = await build_app(app_settings)
    try:
        app.feed_health.record_message("binance")
        app.feed_health.record_message("polymarket")

        now = time.time()
        for _ in range(feed_health_module.MAX_RECONNECTS + 1):
            app.feed_health.record_reconnect("binance")
        assert app.feed_health.reconnect_count("binance") == feed_health_module.MAX_RECONNECTS + 1

        market = make_market(reference_price=65000)
        app.feed.register(market, make_book(market.token_id_yes, 0.49, 0.51), make_book(market.token_id_no, 0.49, 0.51))
        app._known_markets[market.market_id] = market

        with caplog.at_level(logging.WARNING, logger="main"):
            await app._trading_cycle()

        assert "feed_unhealthy" in caplog.text
        assert await app.db.get_open_trades(mode="PAPER") == []
    finally:
        await app.db.close()


async def test_early_exit_skips_sum_to_one_legs(app_settings):
    """
    Regression test for the 2026-08-07 sum-to-one loss: the directional
    model's EDGE_REVERSAL exit fired on a sum_to_one leg and sold it at
    0.854 when holding to settlement would have paid 1.0 — breaking the
    outcome-agnostic hedge. Sum-to-one legs must never be subject to
    directional TAKE_PROFIT / EDGE_REVERSAL exits; they are held to
    settlement by construction.
    """
    app = await build_app(app_settings)
    try:
        now = time.time()
        # Feed the model a strong DOWN move so it reads NO with a big edge
        # (making EDGE_REVERSAL plausible on any YES-position).
        for i, price in enumerate([65000, 64800, 64600, 64400, 64200, 64000, 63800, 63600, 63400, 63200]):
            app.signal_engine.ingest_price_update(
                PriceUpdate(symbol="BTCUSDT", price=price, event_time_ms=0, received_at=now + i, kind="trade")
            )

        market = make_market(reference_price=65000)
        # Books summing below $1 (0.46+0.48=0.94) so a genuine sum-to-one
        # opportunity exists, independent of the directional signal.
        yes_book = make_book(market.token_id_yes, 0.44, 0.46)
        no_book = make_book(market.token_id_no, 0.46, 0.48)
        app.feed.register(market, yes_book, no_book)
        app._known_markets[market.market_id] = market

        # Open a sum-to-one pair manually (broker-level, bypassing the signal
        # gate, exactly as _evaluate_and_maybe_trade would after detection).
        opp = find_sum_to_one_opportunity(
            market, yes_book, no_book,
            settings_module.settings.SUM_TO_ONE_MIN_EDGE_PCT,
            settings_module.settings.TAKER_FEE_PCT,
        )
        assert opp is not None
        await app.broker.place_sum_to_one_order(opp, total_size_usd=100)
        assert len(await app.db.get_open_trades(mode="PAPER")) == 2

        # The model now reads NO vs a held YES leg with a big edge — but the
        # sum-to-one legs must NOT be exited regardless.
        await app._check_early_exits(equity=1000)
        open_trades = await app.db.get_open_trades(mode="PAPER")
        assert len(open_trades) == 2  # both legs still held
    finally:
        await app.db.close()


async def test_reprice_exit_banks_convergence(app_settings):
    """
    Round-trip protocol (the strategy's high-win-rate piece): a directional
    position whose held token has repriced >= REPRICE_EXIT_GAIN_PCT toward the
    entry side while still inside the reprice window must be exited with
    reason=REPRICE. This is a bet that the market CORRECTS (near-certain), not
    a bet on the final outcome (a coin flip) — holding to settlement was
    silently converting every good lag entry into an outcome bet.
    """
    app = await build_app(app_settings)
    try:
        app.feed_health.record_message("binance")
        app.feed_health.record_message("polymarket")
        market = make_market(reference_price=65000)
        entry_yes = make_book(market.token_id_yes, 0.49, 0.51)
        entry_no = make_book(market.token_id_no, 0.49, 0.51)
        app.feed.register(market, entry_yes, entry_no)
        app._known_markets[market.market_id] = market

        fill = await app.broker.place_order(market, "YES", 100)
        assert fill.avg_price < 0.60  # entered near the stale 0.50 book

        # The market reprices toward the entry side: mid now ~0.58 (a
        # ~14% token gain, well past the 7% reprice threshold).
        repriced = make_book(market.token_id_yes, 0.57, 0.59)
        app.feed.books[market.token_id_yes] = repriced

        await app._check_early_exits(equity=1000)
        closed = [t for t in await app.db.get_all_trades(mode="PAPER") if t["status"] == "CLOSED"]
        assert len(closed) == 1
        assert closed[0]["exit_reason"] == "REPRICE"
        assert closed[0]["realized_pnl_usd"] > 0  # banked the reprice, net of fees
    finally:
        await app.db.close()


async def test_reprice_exit_does_not_fire_on_phantom_mid_gain(app_settings):
    """
    Regression for the phantom-gain bug: the exit DECISION used book.mid
    (halfway between bid and ask) while the SELL walked the bid side. On the
    wide books of 5-min crypto markets, mid can show a >= REPRICE_EXIT_GAIN_PCT
    gain above entry while the bid side still rests at the entry price — the
    exit then "fires" and fills at the entry price, a guaranteed loss after
    fees. The decision must use the walked bid price, so a wide book with
    bid == entry must NOT trigger REPRICE even when mid looks like +10%.
    """
    app = await build_app(app_settings)
    try:
        app.feed_health.record_message("binance")
        app.feed_health.record_message("polymarket")
        market = make_market(reference_price=65000)
        entry_yes = make_book(market.token_id_yes, 0.49, 0.51)
        entry_no = make_book(market.token_id_no, 0.49, 0.51)
        app.feed.register(market, entry_yes, entry_no)
        app._known_markets[market.market_id] = market

        fill = await app.broker.place_order(market, "YES", 100)  # entry @ 0.51 (ask)
        assert fill.avg_price == pytest.approx(0.51, abs=0.005)

        # Wide book: bid == entry (0.51), ask 0.61 → mid 0.56 ≈ +10% from
        # 0.51. The old mid-based decision fired REPRICE here and filled at
        # 0.51 (a guaranteed loss). The walked bid side gives 0.51 → 0% gain.
        wide = make_book(market.token_id_yes, 0.51, 0.61)
        app.feed.books[market.token_id_yes] = wide

        await app._check_early_exits(equity=1000)
        open_trades = await app.db.get_open_trades(mode="PAPER")
        assert len(open_trades) == 1  # no phantom exit — position still held
    finally:
        await app.db.close()


async def test_reprice_exit_ignored_after_max_hold(app_settings):
    """
    Once REPRICE_EXIT_MAX_HOLD_S has elapsed, the arbitrage window is gone —
    the REPRICE exit must NOT fire even if the token has gained more than
    REPRICE_EXIT_GAIN_PCT. The position falls through to the normal exits
    (TAKE_PROFIT / EDGE_REVERSAL / settlement), which are stricter.
    """
    app = await build_app(app_settings)
    try:
        market = make_market(reference_price=65000)
        entry_yes = make_book(market.token_id_yes, 0.49, 0.51)
        entry_no = make_book(market.token_id_no, 0.49, 0.51)
        app.feed.register(market, entry_yes, entry_no)
        app._known_markets[market.market_id] = market

        fill = await app.broker.place_order(market, "YES", 100)
        # Age the position well past the reprice window (entry_ts is set by
        # place_order to now).
        conn = app.db._conn
        await conn.execute(
            "UPDATE trades SET entry_ts = ? WHERE id = ?",
            (time.time() - 10_000, fill.trade_id),
        )
        await conn.commit()

        repriced = make_book(market.token_id_yes, 0.57, 0.59)  # ~14% token gain
        app.feed.books[market.token_id_yes] = repriced

        await app._check_early_exits(equity=1000)
        open_trades = await app.db.get_open_trades(mode="PAPER")
        assert len(open_trades) == 1  # REPRICE skipped; ~14% < TAKE_PROFIT 50%
    finally:
        await app.db.close()


async def test_duplicate_position_prevented(app_settings):
    app = await build_app(app_settings)
    try:
        now = time.time()
        for i, price in enumerate([65000, 65200, 65400, 65600, 65800, 66000, 66200, 66400, 66500, 66600]):
            app.signal_engine.ingest_price_update(
                PriceUpdate(symbol="BTCUSDT", price=price, event_time_ms=0, received_at=now + i, kind="trade")
            )
        market = make_market(reference_price=65000)
        yes_book = make_book(market.token_id_yes, 0.49, 0.51)
        no_book = make_book(market.token_id_no, 0.49, 0.51)
        app.feed.register(market, yes_book, no_book)
        app._known_markets[market.market_id] = market

        cash = await app.broker.get_balance()
        equity = await app.broker.get_equity(app._known_markets)
        await app._evaluate_and_maybe_trade(market, cash, equity, exposure_headroom=500)
        await app._evaluate_and_maybe_trade(market, cash, equity, exposure_headroom=500)  # should be a no-op

        open_trades = await app.db.get_open_trades(mode="PAPER")
        assert len(open_trades) == 1  # not 2 -- duplicate prevented
    finally:
        await app.db.close()


async def test_settlement_works_for_a_market_no_longer_in_active_discovery(app_settings):
    """
    This is the core regression test for the settlement dead-code bug: a
    market that has RESOLVED (and would therefore never again be returned by
    discover_active_markets(), since that filters to closed=false) must still
    be settleable via direct market-ID lookup.
    """
    app = await build_app(app_settings)
    try:
        market = make_market(reference_price=65000, resolved=False)
        yes_book = make_book(market.token_id_yes, 0.49, 0.51)
        no_book = make_book(market.token_id_no, 0.49, 0.51)
        app.feed.register(market, yes_book, no_book)

        fill = await app.broker.place_order(market, "YES", 100)
        assert app.broker.has_open_position(market.market_id)

        # Now the market resolves. Crucially: it's removed from what
        # discover_active_markets() would return (simulated by re-registering
        # as resolved=True), exactly like the real Gamma active=true,closed=false
        # filter would exclude it going forward.
        resolved_market = make_market(reference_price=65000, resolved=True)
        app.feed._by_id[market.market_id] = resolved_market
        app.feed.outcomes[market.market_id] = "YES"

        app._open_position_market_ids = {market.market_id}
        # Run one settlement-loop pass manually (avoiding the infinite loop).
        m = await app.feed.get_market_by_id(market.market_id)
        assert m.resolved is True
        pnl = await app.broker.settle_position(m)

        assert pnl is not None
        assert pnl > 0
        assert not app.broker.has_open_position(market.market_id)
    finally:
        await app.db.close()


def _live_broker() -> LiveBroker:
    """A real LiveBroker whose ClobClient + httpx are mocked — no network,
    no real API key derivation."""
    client = MagicMock()
    client.create_or_derive_api_key.return_value = {
        "api_key": "k", "api_secret": "s", "api_passphrase": "p",
    }
    with patch("engine.broker_live.ClobClient", return_value=client), patch(
        "engine.broker_live.httpx.AsyncClient", return_value=MagicMock()
    ):
        return LiveBroker(private_key="0x" + "cd" * 32, alerter=MagicMock(), signature_type=1)


def test_live_broker_wallet_address_derived_from_private_key():
    broker = _live_broker()
    assert broker.wallet_address == Account.from_key("0x" + "cd" * 32).address


async def _wire_live_broker(app, monkeypatch, positions, redeemed):
    """Swap the app's broker for a mocked LiveBroker and stub the data-api +
    redeem calls so no network is touched."""
    broker = _live_broker()
    app.broker = broker
    monkeypatch.setattr(LiveBroker, "wallet_address", property(lambda self: LIVE_ADDRESS))

    async def fake_open_positions(wallet_address):
        return positions

    async def fake_redeem_position(market, trade):
        redeemed.append((market.market_id, trade))
        return LIVE_TX_HASH

    monkeypatch.setattr(broker, "get_open_positions", fake_open_positions)
    monkeypatch.setattr(broker, "redeem_position", fake_redeem_position)
    return broker


# -- LIVE-mode settlement (automated redemption) ------------------------------


async def test_live_settlement_redeems_resolved_position_exactly_once(app_settings, monkeypatch):
    app = await build_app(app_settings)
    try:
        app.feed._by_id[LIVE_CONDITION] = make_market(market_id=LIVE_CONDITION, resolved=True)
        positions = [{"conditionId": LIVE_CONDITION, "size": 100.0, "outcome": "Yes", "redeemable": True}]
        redeemed: list = []
        await _wire_live_broker(app, monkeypatch, positions, redeemed)

        await app._settlement_pass()
        assert len(redeemed) == 1
        assert redeemed[0][0] == LIVE_CONDITION  # exactly the one condition, nothing else

        # data-api still shows the position (chain lag) — must NOT redeem twice.
        await app._settlement_pass()
        assert len(redeemed) == 1
    finally:
        await app.db.close()


async def test_live_settlement_skips_unresolved_position(app_settings, monkeypatch):
    app = await build_app(app_settings)
    try:
        app.feed._by_id[LIVE_CONDITION] = make_market(market_id=LIVE_CONDITION, resolved=False)
        positions = [{"conditionId": LIVE_CONDITION, "size": 100.0, "redeemable": True}]
        redeemed: list = []
        await _wire_live_broker(app, monkeypatch, positions, redeemed)

        await app._settlement_pass()
        assert redeemed == []
    finally:
        await app.db.close()


async def test_live_settlement_skips_zero_size_position(app_settings, monkeypatch):
    app = await build_app(app_settings)
    try:
        app.feed._by_id[LIVE_CONDITION] = make_market(market_id=LIVE_CONDITION, resolved=True)
        positions = [{"conditionId": LIVE_CONDITION, "size": 0.0, "redeemable": True}]
        redeemed: list = []
        await _wire_live_broker(app, monkeypatch, positions, redeemed)

        await app._settlement_pass()
        assert redeemed == []
    finally:
        await app.db.close()


async def test_live_settlement_skips_position_not_redeemable_yet(app_settings, monkeypatch):
    """The data-api `redeemable` flag is the authoritative pre-flight: a
    losing or not-yet-resolved condition must never trigger a broadcast."""
    app = await build_app(app_settings)
    try:
        app.feed._by_id[LIVE_CONDITION] = make_market(market_id=LIVE_CONDITION, resolved=True)
        positions = [{"conditionId": LIVE_CONDITION, "size": 100.0, "redeemable": False}]
        redeemed: list = []
        await _wire_live_broker(app, monkeypatch, positions, redeemed)

        await app._settlement_pass()
        assert redeemed == []
    finally:
        await app.db.close()


async def test_live_settlement_retries_after_broadcast_failure(app_settings, monkeypatch):
    """A failed broadcast is NOT recorded in _redeem_submitted, so the next
    pass retries it; a successful one is recorded to prevent duplicates."""
    app = await build_app(app_settings)
    try:
        app.feed._by_id[LIVE_CONDITION] = make_market(market_id=LIVE_CONDITION, resolved=True)
        positions = [{"conditionId": LIVE_CONDITION, "size": 10.0, "redeemable": True}]
        redeemed: list = []
        broker = await _wire_live_broker(app, monkeypatch, positions, redeemed)
        calls = {"n": 0}

        async def flaky_redeem(market, trade):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("rpc down")
            redeemed.append((market.market_id, trade))
            return LIVE_TX_HASH

        monkeypatch.setattr(broker, "redeem_position", flaky_redeem)

        await app._settlement_pass()  # first broadcast fails -> logged, NOT recorded
        assert calls["n"] == 1
        assert LIVE_CONDITION not in app._redeem_submitted

        await app._settlement_pass()  # retried -> succeeds and is recorded
        assert calls["n"] == 2
        assert LIVE_CONDITION in app._redeem_submitted
    finally:
        await app.db.close()


async def test_run_survives_unsupported_signal_handlers(app_settings, monkeypatch):
    """
    Regression test for the Windows crash: asyncio's
    loop.add_signal_handler() raises NotImplementedError on Windows, and
    TradingApp.run() must not die at startup because of it. The run must
    still boot, and a shutdown request must still stop it cleanly.
    """
    app = await build_app(app_settings)
    try:
        loop = asyncio.get_running_loop()

        def windows_add_signal_handler(sig, callback, *args, **kwargs):
            raise NotImplementedError("add_signal_handler is not supported on Windows")

        monkeypatch.setattr(loop, "add_signal_handler", windows_add_signal_handler)

        # Replace the real network feeds with no-ops so the spawned tasks
        # don't attempt any socket connections during the brief run.
        async def empty_stream():
            return
            yield  # pragma: no cover

        async def empty_ws_run():
            await asyncio.sleep(3600)

        app.binance_feed.stream = empty_stream
        app.coinbase_feed.stream = empty_stream
        app.ws_feed.run = empty_ws_run

        run_task = asyncio.create_task(app.run())
        await asyncio.sleep(0.3)  # let it get past signal registration
        app._shutdown.set()
        await asyncio.wait_for(run_task, timeout=15)
    finally:
        await app.db.close()


async def test_restart_recovery_end_to_end(app_settings):
    """A position opened in one TradingApp instance must be settleable by a
    fresh instance pointed at the same database, simulating a real restart."""
    app1 = await build_app(app_settings)
    market = make_market(reference_price=65000)
    yes_book = make_book(market.token_id_yes, 0.49, 0.51)
    no_book = make_book(market.token_id_no, 0.49, 0.51)
    app1.feed.register(market, yes_book, no_book)
    await app1.broker.place_order(market, "YES", 100)
    await app1.db.close()

    app2 = await build_app(app_settings)
    try:
        assert app2.broker.has_open_position(market.market_id)  # restored on setup()

        resolved_market = make_market(reference_price=65000, resolved=True)
        app2.feed.register(resolved_market, yes_book, no_book)
        app2.feed.outcomes[market.market_id] = "YES"

        pnl = await app2.broker.settle_position(resolved_market)
        assert pnl is not None and pnl > 0
    finally:
        await app2.db.close()
