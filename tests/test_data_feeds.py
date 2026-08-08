"""
Tests for data/polymarket_feed.py and data/binance_feed.py parsing logic,
using recorded fixture data. These tests never hit a live endpoint.
"""
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import websockets.exceptions

from data.binance_feed import BinanceFeed, PriceUpdate, _parse_message
from data.polymarket_feed import (
    OrderBook,
    OrderBookLevel,
    PolymarketFeed,
    _duration_from_time_range,
    _parse_gamma_market,
)

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


def test_parse_gamma_market_handles_json_string_clob_token_ids():
    """
    The /events/keyset response (used by discovery since 2026-08-04) encodes
    clobTokenIds as a JSON string, not a list. Regression guard: parsing must
    extract the real token IDs either way — a naive `tokens[0]` on the string
    yielded '[' and '"', which silently broke the CLOB book fetch and the WS
    subscription.
    """
    raw = {
        "id": "559700",
        "question": "Ethereum Up or Down - December 19, 11:30AM-11:35AM ET",
        "clobTokenIds": json.dumps(["tok_yes_eth_live", "tok_no_eth_live"]),
        "liquidity": "125000.50",
        "endDate": "2026-12-19T16:35:00Z",
        "closed": False,
    }
    market = _parse_gamma_market(raw)
    assert market is not None
    assert market.token_id_yes == "tok_yes_eth_live"
    assert market.token_id_no == "tok_no_eth_live"


# -- Live Gamma title format (timestamp window, not "5 min"/"15 min" text) --
# Polymarket's current up/down titles are e.g. "Ethereum Up or Down -
# December 19, 11:30AM-11:35AM ET". The old parser only matched "5 min"/
# "15 min" text and silently returned 0 markets against live data — this
# format is the regression guard.


def test_parse_gamma_market_live_timestamp_format_5min():
    raw = {
        "id": "559700",
        "question": "Ethereum Up or Down - December 19, 11:30AM-11:35AM ET",
        "clobTokenIds": ["tok_yes_eth_live", "tok_no_eth_live"],
        "liquidity": "125000.50",
        "endDate": "2026-12-19T16:35:00Z",
        "closed": False,
    }
    market = _parse_gamma_market(raw)
    assert market is not None
    assert market.asset == "ETH"
    assert market.duration_minutes == 5


def test_parse_gamma_market_live_timestamp_format_15min():
    raw = {
        "id": "559701",
        "question": "Bitcoin Up or Down - December 19, 11:30AM-11:45AM ET",
        "clobTokenIds": ["tok_yes_btc_live", "tok_no_btc_live"],
        "liquidity": "250000.00",
        "endDate": "2026-12-19T16:45:00Z",
        "closed": False,
    }
    market = _parse_gamma_market(raw)
    assert market is not None
    assert market.asset == "BTC"
    assert market.duration_minutes == 15


def test_parse_gamma_market_rejects_non_updown_live_title():
    """A BTC question without a duration (text or time window) must still be
    rejected — e.g. long-dated 'Will the price of Bitcoin be above $70k'."""
    raw = {
        "id": "559702",
        "question": "Will the price of Bitcoin be above $70,000 on August 4?",
        "clobTokenIds": ["tok_yes_btc", "tok_no_btc"],
        "liquidity": "100000.00",
        "endDate": "2026-08-04T00:00:00Z",
        "closed": False,
    }
    assert _parse_gamma_market(raw) is None


@pytest.mark.parametrize(
    "question,expected",
    [
        ("Up or Down - December 19, 11:30AM-11:35AM ET", 5),
        ("Up or Down - December 19, 11:30AM-11:45AM ET", 15),
        ("Up or Down - 11:55AM-12:05PM ET", None),  # 10 min, not 5/15
        ("Up or Down - 12:00PM-12:05PM ET", 5),  # noon crossing
        ("Up or Down - 12:00AM-12:15AM ET", 15),  # midnight crossing
        ("Up or Down - 5 min - 14:00 ET", None),  # no window, keyword path
        ("Will it rain in NYC tomorrow?", None),
    ],
)
def test_duration_from_time_range(question: str, expected):
    assert _duration_from_time_range(question) == expected


async def test_discover_active_markets_uses_keyset_endpoint_with_updown_tag():
    """
    Regression guard for the 2026-08-04 live-data bug: the old /markets list
    endpoint is deprecated/sunset (API returns `sunset: Fri, 01 May 2026` +
    `warning: use /markets/keyset`) and serves stale Dec-2025 ghost rows, so
    the bot found zero markets and could never trade. Discovery must use
    /events/keyset with tag_slug=up-or-down, ordered endDate-ascending so the
    soonest-ending (currently live) windows come first.
    """
    captured = {}

    async def fake_gamma_get(path, params=None):
        captured["path"] = path
        captured["params"] = params
        return []

    feed = PolymarketFeed(min_liquidity_usd=1)
    feed._gamma_get = fake_gamma_get  # type: ignore[method-assign]
    await feed.discover_active_markets()

    assert captured["path"] == "/events/keyset"
    assert captured["params"]["tag_slug"] == "up-or-down"
    assert captured["params"]["order"] == "endDate"
    assert captured["params"]["ascending"] == "true"
    assert captured["params"]["active"] == "true"


def _keyset_event(market: dict, event_id: str = "ev1") -> dict:
    return {"id": event_id, "title": market["question"], "markets": [market]}


async def test_discover_active_markets_parses_nested_keyset_events():
    """The keyset response nests markets inside events; discovery must flatten
    them, keep only live windows within the lookahead, and dedupe."""
    now = time.time()

    def iso(offset_s: float) -> str:
        return datetime.fromtimestamp(now + offset_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    live_btc = {
        "id": "111",
        "question": "Bitcoin Up or Down - August 4, 7:05AM-7:10AM ET",
        "clobTokenIds": ["t_yes_1", "t_no_1"],
        "liquidity": "20000",
        "endDate": iso(300),  # ending in 5 min
        "closed": False,
    }
    far_future_btc = {
        "id": "222",
        "question": "Bitcoin Up or Down - August 6, 6:40AM-6:45AM ET",
        "clobTokenIds": ["t_yes_2", "t_no_2"],
        "liquidity": "50000",
        "endDate": iso(2 * 86400),  # ends in 2 days — pre-created, not live
        "closed": False,
    }
    thin_live_btc = {
        "id": "333",
        "question": "Bitcoin Up or Down - August 4, 7:05AM-7:10AM ET",
        "clobTokenIds": ["t_yes_3", "t_no_3"],
        "liquidity": "800",  # below the 5k floor
        "endDate": iso(300),
        "closed": False,
    }

    payload = {
        "$schema": "x",
        "events": [
            _keyset_event(live_btc, "e1"),
            _keyset_event(far_future_btc, "e2"),
            _keyset_event(thin_live_btc, "e3"),
            _keyset_event(live_btc, "e1"),  # duplicate market, same id
        ],
        "next_cursor": None,
    }

    async def fake_gamma_get(path, params=None):
        return payload

    feed = PolymarketFeed(min_liquidity_usd=5_000.0)
    feed._gamma_get = fake_gamma_get  # type: ignore[method-assign]
    markets = await feed.discover_active_markets()

    # Only the live, sufficiently-liquid market survives; the far-future
    # pre-created one and the thin one are dropped; the duplicate is deduped.
    assert [m.market_id for m in markets] == ["111"]


async def test_discover_active_markets_handles_plain_list_payload():
    """Defensive: some Gamma endpoints return a bare list; discovery must not
    crash on that shape."""

    async def fake_gamma_get(path, params=None):
        return []

    feed = PolymarketFeed(min_liquidity_usd=1)
    feed._gamma_get = fake_gamma_get  # type: ignore[method-assign]
    assert await feed.discover_active_markets() == []


async def test_discover_pages_past_ghost_crowd_to_find_live_windows():
    """
    Regression guard for the 2026-08-06 live-data bug: /events/keyset serves
    inconsistent cached slices — sometimes the live up/down windows are buried
    behind ~1,400 stale Dec-2025 ghost events still flagged active=true. With
    a 5-page cap discovery gave up before reaching them (found 0 markets while
    the bot sat idle). It must keep paging through the ghosts (using
    next_cursor) until it reaches the live windows, then filter them in.
    """
    now = time.time()

    def iso(offset_s: float) -> str:
        return datetime.fromtimestamp(now + offset_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    ghost = {
        "id": "999",
        "question": "Bitcoin Up or Down - December 19, 11:30AM-11:35AM ET",
        "clobTokenIds": ["t_g_yes", "t_g_no"],
        "liquidity": "0",
        "endDate": "2025-12-19T16:35:00Z",  # stale ghost, far in the past
        "closed": False,
    }
    live = {
        "id": "555",
        "question": "Bitcoin Up or Down - August 4, 7:05AM-7:10AM ET",
        "clobTokenIds": ["t_l_yes", "t_l_no"],
        "liquidity": "12000",
        "endDate": iso(300),  # live window, ending in 5 min
        "closed": False,
    }

    calls = []

    async def fake_gamma_get(path, params=None):
        calls.append(dict(params or {}))
        # Page 1: ghosts only. Page 2: the live window. Any later: empty.
        if params and params.get("cursor"):
            return {"events": [_keyset_event(live, "e-live")], "next_cursor": None}
        return {
            "events": [_keyset_event(ghost, "e-g")] * 3,
            "next_cursor": "cursor-2",
        }

    feed = PolymarketFeed(min_liquidity_usd=1_000.0)
    feed._gamma_get = fake_gamma_get  # type: ignore[method-assign]
    markets = await feed.discover_active_markets()

    # It must have paginated (used the cursor), and only the live window
    # survives — the Dec-2025 ghost is filtered by both liquidity and horizon.
    assert len(calls) >= 2
    assert calls[1]["cursor"] == "cursor-2"
    assert [m.market_id for m in markets] == ["555"]

async def test_discover_stops_on_repeated_cursor():
    """
    Regression guard for the 2026-08-08 live bug: /events/keyset repeatedly
    served the SAME next_cursor for the same page, so discovery re-requested
    that page up to MAX_DISCOVERY_PAGES times every cycle — and with two bot
    processes that became ~10 identical requests/sec (16MB of log in under a
    day). A cursor that repeats means every later page is identical: stop.
    """
    now = time.time()

    def iso(offset_s: float) -> str:
        return datetime.fromtimestamp(now + offset_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    live = {
        "id": "555",
        "question": "Bitcoin Up or Down - August 4, 7:05AM-7:10AM ET",
        "clobTokenIds": ["t_l_yes", "t_l_no"],
        "liquidity": "12000",
        "endDate": iso(300),
        "closed": False,
    }

    calls = []

    async def fake_gamma_get(path, params=None):
        calls.append((params or {}).get("cursor"))
        # Every response — including the first — echoes the same cursor, the
        # exact stuck-cursor behavior seen live.
        return {"events": [_keyset_event(live, "e-live")], "next_cursor": "stuck-cursor"}

    feed = PolymarketFeed(min_liquidity_usd=1_000.0)
    feed._gamma_get = fake_gamma_get  # type: ignore[method-assign]
    markets = await feed.discover_active_markets()

    # One request for the initial page; the repeated cursor must stop it.
    assert len(calls) == 2
    assert calls == [None, "stuck-cursor"]
    assert [m.market_id for m in markets] == ["555"]


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


# -- Resolved-outcome parsing (settlement) -------------------------------------

async def test_get_market_outcome_parses_outcome_prices_yes_wins():
    """
    The Gamma market payload no longer carries the legacy `outcome` field —
    resolution is now `outcomes` + `outcomePrices` + `umaResolutionStatus`.
    Regression guard: settlement must read the winner out of outcomePrices
    (index 0 = YES token, index 1 = NO token) or positions stay OPEN forever.
    """
    raw = {
        "id": "559700",
        "question": "Bitcoin Up or Down - August 4, 7:05AM-7:10AM ET",
        "clobTokenIds": ["tok_yes", "tok_no"],
        "outcomes": ["Up", "Down"],
        "outcomePrices": ["1", "0"],  # Up won -> YES token won
        "umaResolutionStatus": "resolved",
        "closed": True,
    }

    async def fake_gamma_get(path, params=None):
        return raw

    feed = PolymarketFeed(min_liquidity_usd=1)
    feed._gamma_get = fake_gamma_get  # type: ignore[method-assign]
    assert await feed.get_market_outcome("559700") == "YES"


async def test_get_market_outcome_parses_outcome_prices_no_wins():
    raw = {
        "id": "559700",
        "question": "Bitcoin Up or Down - August 4, 7:05AM-7:10AM ET",
        "clobTokenIds": ["tok_yes", "tok_no"],
        "outcomes": ["Up", "Down"],
        "outcomePrices": ["0", "1"],  # Down won -> NO token won
        "umaResolutionStatus": "resolved",
        "closed": True,
    }

    async def fake_gamma_get(path, params=None):
        return raw

    feed = PolymarketFeed(min_liquidity_usd=1)
    feed._gamma_get = fake_gamma_get  # type: ignore[method-assign]
    assert await feed.get_market_outcome("559700") == "NO"


async def test_get_market_outcome_handles_json_string_prices():
    """
    Verified live 2026-08-06: Gamma encodes outcomePrices as a JSON STRING
    (e.g. '"["0","1"]"'), not a list — same shape trap as clobTokenIds. A
    plain isinstance(prices, list) check silently returns None and settlement
    never fires. Regression guard for the normalized shape.
    """
    raw = {
        "id": "559700",
        "question": "Bitcoin Up or Down - August 4, 7:05AM-7:10AM ET",
        "clobTokenIds": ["tok_yes", "tok_no"],
        "outcomes": ["Up", "Down"],
        "outcomePrices": json.dumps(["0", "1"]),  # JSON string, Down won
        "umaResolutionStatus": "resolved",
        "closed": True,
    }

    async def fake_gamma_get(path, params=None):
        return raw

    feed = PolymarketFeed(min_liquidity_usd=1)
    feed._gamma_get = fake_gamma_get  # type: ignore[method-assign]
    assert await feed.get_market_outcome("559700") == "NO"


async def test_get_market_outcome_returns_none_while_pending():
    """A not-yet-resolved market must return None so the caller retries later
    instead of settling at a wrong outcome."""
    raw = {
        "id": "559700",
        "question": "Bitcoin Up or Down - August 4, 7:05AM-7:10AM ET",
        "clobTokenIds": ["tok_yes", "tok_no"],
        "outcomes": ["Up", "Down"],
        "outcomePrices": ["0.4", "0.6"],  # still trading, not resolved
        "umaResolutionStatus": None,
        "closed": False,
    }

    async def fake_gamma_get(path, params=None):
        return raw

    feed = PolymarketFeed(min_liquidity_usd=1)
    feed._gamma_get = fake_gamma_get  # type: ignore[method-assign]
    assert await feed.get_market_outcome("559700") is None


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
