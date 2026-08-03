"""
Cross-exchange comparison logic driven through FAKE/MOCKED price feeds.

Everything here replaces the Binance/Coinbase WebSocket feeds with scripted
stand-ins (FakePriceFeed) or unittest.mock objects, consumed exactly the way
main.py consumes them (`async for update in feed.stream()` -> engine
ingest). No test in this file touches a real network connection — the real
feed classes are only ever exercised at their constructor (which performs no
I/O) or replaced entirely by mocks.
"""
import time
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from config.settings import Settings
from data.binance_feed import PriceUpdate
from data.coinbase_feed import PriceUpdate as CoinbasePriceUpdate
from data.polymarket_feed import Market, OrderBook, OrderBookLevel
from engine.signal import SignalEngine
from main import TradingApp
from storage.db import Database


def make_settings(**overrides) -> Settings:
    defaults = dict(EDGE_THRESHOLD_PCT=0.05, MIN_CONFIDENCE=0.3, MIN_MARKET_LIQUIDITY_USD=50_000)
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def make_market(reference_price=None) -> Market:
    return Market(
        market_id="m1", question="Bitcoin Up or Down - 15 min",
        token_id_yes="tok_yes", token_id_no="tok_no",
        liquidity_usd=100_000, end_date_iso="2026-07-31T14:00:00Z",
        asset="BTC", duration_minutes=15,
        reference_price=reference_price, expires_at_ts=time.time() + 300,
    )


def make_book(token_id: str, best_bid: float, best_ask: float, depth=200_000) -> OrderBook:
    size = depth / ((best_bid + best_ask) / 2)
    return OrderBook(
        market_id="m1", token_id=token_id,
        bids=(OrderBookLevel(price=best_bid, size=size),),
        asks=(OrderBookLevel(price=best_ask, size=size),),
    )


class FakePriceFeed:
    """
    Scripted stand-in for BinanceFeed/CoinbaseFeed. Implements the same
    `stream()` async-generator interface main.py's ingest loops consume:
    yields the given updates in order, then ends. No sockets, no reconnect
    loop — purely a test double.
    """

    def __init__(self, updates):
        self._updates = list(updates)

    async def stream(self):
        for u in self._updates:
            yield u


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    yield database
    await database.close()


def binance_tick(price: float, received_at: Optional[float] = None) -> PriceUpdate:
    return PriceUpdate(
        symbol="BTCUSDT", price=price, event_time_ms=0,
        received_at=received_at if received_at is not None else time.time(), kind="trade",
    )


def coinbase_tick(price: float, received_at: Optional[float] = None) -> CoinbasePriceUpdate:
    return CoinbasePriceUpdate(
        symbol="BTCUSDT", price=price, event_time_ms=0,
        received_at=received_at if received_at is not None else time.time(), kind="trade",
    )


async def run_ingest(engine: SignalEngine, feed, source: str) -> None:
    """Consume a feed exactly like main.py's _binance/_coinbase_ingest_loop:
    `async for update in feed.stream(): engine.ingest_price_update(...)`.
    `feed` is anything exposing a stream() async generator — a FakePriceFeed
    or a mocked feed object."""
    async for update in feed.stream():
        engine.ingest_price_update(update, source=source)


# -- the comparison logic, fed through fake feeds ------------------------------


async def test_fake_feeds_drive_disagreement_comparison(db):
    """The full feed->engine path: scripted feeds are consumed via stream()
    and the engine computes the disagreement from them."""
    settings = make_settings()
    engine = SignalEngine(settings, db)
    now = time.time()
    await run_ingest(engine, FakePriceFeed([binance_tick(100.0, now), binance_tick(101.0, now + 1), binance_tick(102.0, now + 2)]), "binance")
    await run_ingest(engine, FakePriceFeed([coinbase_tick(110.0)]), "coinbase")

    assert engine.cross_exchange_disagreement_pct("BTCUSDT") == pytest.approx(abs(110.0 - 102.0) / 102.0 * 100.0)

    market = make_market(reference_price=100)
    yes_book = make_book("tok_yes", 0.38, 0.40)
    no_book = make_book("tok_no", 0.60, 0.62)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.fired is False
    assert signal.reason == "cross_exchange_disagreement"


async def test_fake_feeds_agree_and_signal_fires(db):
    settings = make_settings()
    engine = SignalEngine(settings, db)
    now = time.time()
    await run_ingest(engine, FakePriceFeed([binance_tick(100.0, now), binance_tick(101.0, now + 1), binance_tick(102.0, now + 2)]), "binance")
    await run_ingest(engine, FakePriceFeed([coinbase_tick(102.01)]), "coinbase")  # 0.01% off — inside tolerance

    market = make_market(reference_price=100)
    yes_book = make_book("tok_yes", 0.38, 0.40)
    no_book = make_book("tok_no", 0.60, 0.62)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.fired is True
    assert signal.reason == "OK"


async def test_coinbase_ticks_never_feed_model_tracker_via_feeds(db):
    """Feeding both fake feeds must keep the model's momentum tracker fed
    ONLY by Binance — Coinbase ticks are gate-only, even through the real
    feed-consumption path."""
    settings = make_settings()
    engine = SignalEngine(settings, db)
    now = time.time()
    await run_ingest(engine, FakePriceFeed([binance_tick(100.0, now), binance_tick(101.0, now + 1)]), "binance")
    await run_ingest(engine, FakePriceFeed([coinbase_tick(110.0)]), "coinbase")

    tracker = engine._trackers["BTCUSDT"]
    assert len(tracker._ticks) == 2  # only the two Binance ticks
    assert all(t.kind == "trade" and t.price < 102.0 for t in tracker._ticks)
    # The gate still sees the Coinbase price.
    assert engine._latest_price[("coinbase", "BTCUSDT")] == 110.0


async def test_empty_coinbase_feed_is_fail_open(db):
    """A feed that yields no ticks (e.g. Coinbase down) makes the gate
    'can't judge' -> signal fires — the bot keeps working on Binance alone."""
    settings = make_settings()
    engine = SignalEngine(settings, db)
    now = time.time()
    await run_ingest(engine, FakePriceFeed([binance_tick(100.0, now), binance_tick(101.0, now + 1), binance_tick(102.0, now + 2)]), "binance")
    await run_ingest(engine, FakePriceFeed([]), "coinbase")  # down: no ticks at all

    market = make_market(reference_price=100)
    yes_book = make_book("tok_yes", 0.38, 0.40)
    no_book = make_book("tok_no", 0.60, 0.62)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.fired is True
    assert signal.reason == "OK"


async def test_disagreement_audit_row_written_through_fake_feeds(db):
    """End-to-end through feeds: an above-threshold disagreement seen via
    fake feeds must land in the exchange_disagreements audit table."""
    settings = make_settings()
    engine = SignalEngine(settings, db)
    now = time.time()
    await run_ingest(engine, FakePriceFeed([binance_tick(100.0, now), binance_tick(101.0, now + 1), binance_tick(102.0, now + 2)]), "binance")
    await run_ingest(engine, FakePriceFeed([coinbase_tick(110.0)]), "coinbase")

    market = make_market(reference_price=100)
    yes_book = make_book("tok_yes", 0.38, 0.40)
    no_book = make_book("tok_no", 0.60, 0.62)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.reason == "cross_exchange_disagreement"

    rows = await db.get_exchange_disagreements(symbol="BTCUSDT")
    assert len(rows) == 1
    assert rows[0]["disagreement_pct"] == pytest.approx(abs(110.0 - 102.0) / 102.0 * 100.0)


# -- mock-based feeds (unittest.mock) ------------------------------------------


async def _scripted_stream(updates):
    for u in updates:
        yield u


async def test_mocked_feed_streams_drive_comparison(db):
    """Feed classes replaced by MagicMock objects whose stream() is a
    scripted async generator — proves the comparison logic works against a
    mocked feed interface and never touches the network."""
    settings = make_settings()
    engine = SignalEngine(settings, db)
    now = time.time()

    bn_feed = MagicMock()
    bn_feed.stream.return_value = _scripted_stream([
        binance_tick(100.0, now), binance_tick(101.0, now + 1), binance_tick(102.0, now + 2),
    ])
    cb_feed = MagicMock()
    cb_feed.stream.return_value = _scripted_stream([coinbase_tick(110.0)])

    await run_ingest(engine, bn_feed, "binance")
    await run_ingest(engine, cb_feed, "coinbase")

    bn_feed.stream.assert_called_once()
    cb_feed.stream.assert_called_once()
    assert engine.cross_exchange_disagreement_pct("BTCUSDT") == pytest.approx(abs(110.0 - 102.0) / 102.0 * 100.0)


async def test_real_ingest_loops_with_mocked_feeds(db):
    """The ACTUAL main.py ingest loops (_binance_ingest_loop /
    _coinbase_ingest_loop) driven with the real feed classes replaced by
    mocks — proving the wiring works and no real WebSocket is opened. The
    patch replaces the constructors, so TradingApp.__init__ already wires the
    mocks into app.binance_feed / app.coinbase_feed."""
    bn_mock = MagicMock()
    bn_mock.stream.return_value = _scripted_stream([binance_tick(100.0), binance_tick(102.0)])
    cb_mock = MagicMock()
    cb_mock.stream.return_value = _scripted_stream([coinbase_tick(110.0)])

    with patch("main.BinanceFeed", return_value=bn_mock), patch("main.CoinbaseFeed", return_value=cb_mock):
        app = TradingApp()
    app.signal_engine = SignalEngine(make_settings(), db)

    await app._binance_ingest_loop()
    await app._coinbase_ingest_loop()

    bn_mock.stream.assert_called_once()
    cb_mock.stream.assert_called_once()
    assert app.signal_engine.cross_exchange_disagreement_pct("BTCUSDT") == pytest.approx(abs(110.0 - 102.0) / 102.0 * 100.0)
    # Coinbase never pollutes the model tracker through the real loops either.
    assert len(app.signal_engine._trackers["BTCUSDT"]._ticks) == 2
