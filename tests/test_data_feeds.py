"""
Tests for data/polymarket_feed.py and data/binance_feed.py parsing logic,
using recorded fixture data. These tests never hit a live endpoint.
"""
import asyncio
import json
import time
from pathlib import Path

import pytest
import websockets.exceptions

from data.binance_feed import BinanceFeed, PriceUpdate, _parse_message
from data.polymarket_feed import OrderBook, OrderBookLevel, _parse_gamma_market

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


# -- Gamma market parsing / liquidity filter -----------------------------------

def test_parse_gamma_market_identifies_btc_15min():
    raw = load_fixture("sample_gamma_markets.json")[0]
    market = _parse_gamma_market(raw)
    assert market is not None
    assert market.asset == "BTC"
    assert market.duration_minutes == 15
    assert market.liquidity_usd == pytest.approx(125000.50)
    assert market.token_id_yes == "tok_yes_btc_1"


def test_parse_gamma_market_identifies_eth_5min():
    raw = load_fixture("sample_gamma_markets.json")[1]
    market = _parse_gamma_market(raw)
    assert market is not None
    assert market.asset == "ETH"
    assert market.duration_minutes == 5


def test_parse_gamma_market_rejects_non_crypto_market():
    raw = load_fixture("sample_gamma_markets.json")[2]
    market = _parse_gamma_market(raw)
    assert market is None  # "will it rain" market should be filtered out


def test_liquidity_filter_excludes_thin_markets():
    """MIN_MARKET_LIQUIDITY_USD filtering is applied by the caller
    (discover_active_markets); this test checks the raw parse + a manual
    filter pass mirrors that logic without hitting the network."""
    raw_markets = load_fixture("sample_gamma_markets.json")
    min_liquidity = 50_000.0
    eligible = []
    for raw in raw_markets:
        m = _parse_gamma_market(raw)
        if m and m.liquidity_usd >= min_liquidity:
            eligible.append(m)
    # Only the BTC market clears $50k liquidity; ETH market ($9k) is filtered out,
    # rain market was already excluded for not being a crypto up/down market.
    assert len(eligible) == 1
    assert eligible[0].asset == "BTC"


# -- Order book parsing / mid / depth --------------------------------------------

def _order_book_from_fixture() -> OrderBook:
    raw = load_fixture("sample_order_book.json")
    bids = tuple(
        OrderBookLevel(price=float(b["price"]), size=float(b["size"]))
        for b in sorted(raw["bids"], key=lambda x: -float(x["price"]))
    )
    asks = tuple(
        OrderBookLevel(price=float(a["price"]), size=float(a["size"]))
        for a in sorted(raw["asks"], key=lambda x: float(x["price"]))
    )
    return OrderBook(market_id="m1", token_id="t1", bids=bids, asks=asks)


def test_order_book_best_bid_ask_and_mid():
    book = _order_book_from_fixture()
    assert book.best_bid == pytest.approx(0.54)
    assert book.best_ask == pytest.approx(0.56)
    assert book.mid == pytest.approx(0.55)


def test_order_book_depth_usd():
    book = _order_book_from_fixture()
    # ask depth over first 2 levels: 0.56*800 + 0.57*2500
    expected = 0.56 * 800 + 0.57 * 2500
    assert book.depth_usd("ask", levels=2) == pytest.approx(expected)


# -- Binance message parsing --------------------------------------------------

def test_parse_trade_message():
    raw = {
        "stream": "btcusdt@trade",
        "data": {"e": "trade", "E": 1700000000000, "s": "BTCUSDT", "p": "67123.45"},
    }
    update = _parse_message(raw)
    assert isinstance(update, PriceUpdate)
    assert update.symbol == "BTCUSDT"
    assert update.price == pytest.approx(67123.45)
    assert update.kind == "trade"


def test_parse_ticker_message():
    raw = {
        "stream": "ethusdt@ticker",
        "data": {"e": "24hrTicker", "E": 1700000000000, "s": "ETHUSDT", "c": "3456.78"},
    }
    update = _parse_message(raw)
    assert update is not None
    assert update.symbol == "ETHUSDT"
    assert update.kind == "ticker"


def test_parse_malformed_message_returns_none():
    raw = {"stream": "btcusdt@trade", "data": {"e": "trade"}}  # missing price
    assert _parse_message(raw) is None


def test_price_update_age_seconds():
    update = PriceUpdate(symbol="BTCUSDT", price=1.0, event_time_ms=0, received_at=time.time() - 5, kind="trade")
    assert update.age_seconds >= 5


# -- FeedHealth reconnect callback ----------------------------------------------

class _DroppingWS:
    """Fake websocket: yields one message then raises ConnectionClosed, so the
    feed's reconnect path (and its on_reconnect callback) actually runs."""

    def __init__(self, raw_message: bytes):
        self._raw_message = raw_message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._raw_message is not None:
            msg, self._raw_message = self._raw_message, None
            return msg
        raise websockets.exceptions.ConnectionClosed(None, None)


async def test_binance_reconnect_callback_fires_after_drop():
    """Drive stream() until the fake drops; on_reconnect must have fired."""
    reconnects = []
    feed = BinanceFeed(on_reconnect=lambda: reconnects.append(1))

    raw = json.dumps({
        "stream": "btcusdt@trade",
        "data": {"e": "trade", "E": 1700000000000, "s": "BTCUSDT", "p": "67123.45"},
    }).encode()

    async def fake_connect():
        return _DroppingWS(raw)

    feed._connect = fake_connect  # type: ignore[method-assign]

    task = asyncio.create_task(_collect_updates(feed))
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert reconnects  # callback fired after the drop


async def _collect_updates(feed) -> None:
    async for _ in feed.stream():
        pass
