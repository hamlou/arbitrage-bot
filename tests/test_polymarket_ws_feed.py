"""
Tests for data/polymarket_ws_feed.py's event-handling logic, using recorded
fixture events. No live WebSocket connection is opened in these tests.
"""
import asyncio
import json
import time
from pathlib import Path

import pytest
import websockets.exceptions

from data.polymarket_ws_feed import PolymarketWSFeed

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def test_book_event_populates_cache():
    feed = PolymarketWSFeed(asset_ids=["tok_yes_btc_1"])
    event = load_fixture("sample_ws_book_event.json")

    feed._handle_event(event)

    book = feed.get_cached_book("tok_yes_btc_1")
    assert book is not None
    assert book.best_bid == pytest.approx(0.54)
    assert book.best_ask == pytest.approx(0.56)
    assert feed.is_fresh("tok_yes_btc_1")


def test_unknown_asset_id_price_change_is_ignored_without_base_book():
    feed = PolymarketWSFeed(asset_ids=["tok_yes_btc_1"])
    # No prior 'book' snapshot exists yet for this asset. Schema verified
    # against a live capture: changes arrive nested under "price_changes".
    feed._handle_event({
        "event_type": "price_change",
        "price_changes": [{
            "asset_id": "tok_yes_btc_1",
            "price": "0.60", "size": "500", "side": "BUY",
        }],
    })
    assert feed.get_cached_book("tok_yes_btc_1") is None


def test_price_change_adds_new_bid_level():
    feed = PolymarketWSFeed(asset_ids=["tok_yes_btc_1"])
    feed._handle_event(load_fixture("sample_ws_book_event.json"))

    feed._handle_event({
        "event_type": "price_change",
        "price_changes": [{
            "asset_id": "tok_yes_btc_1",
            "price": "0.545", "size": "100", "side": "BUY",
        }],
    })

    book = feed.get_cached_book("tok_yes_btc_1")
    prices = [lvl.price for lvl in book.bids]
    assert 0.545 in prices
    # Bids must stay sorted best-first (highest price first).
    assert prices == sorted(prices, reverse=True)


def test_price_change_with_zero_size_removes_level():
    feed = PolymarketWSFeed(asset_ids=["tok_yes_btc_1"])
    feed._handle_event(load_fixture("sample_ws_book_event.json"))
    assert any(lvl.price == pytest.approx(0.54) for lvl in feed.get_cached_book("tok_yes_btc_1").bids)

    feed._handle_event({
        "event_type": "price_change",
        "price_changes": [{
            "asset_id": "tok_yes_btc_1",
            "price": "0.54", "size": "0", "side": "BUY",
        }],
    })

    book = feed.get_cached_book("tok_yes_btc_1")
    assert not any(lvl.price == pytest.approx(0.54) for lvl in book.bids)


def test_is_fresh_false_when_never_updated():
    feed = PolymarketWSFeed(asset_ids=["tok_yes_btc_1"])
    assert feed.is_fresh("tok_yes_btc_1") is False


def test_is_fresh_false_after_max_age_exceeded():
    feed = PolymarketWSFeed(asset_ids=["tok_yes_btc_1"])
    feed._handle_event(load_fixture("sample_ws_book_event.json"))
    feed._last_update_at["tok_yes_btc_1"] = time.time() - 999
    assert feed.is_fresh("tok_yes_btc_1", max_age_s=5.0) is False


def test_unhandled_event_types_do_not_raise():
    feed = PolymarketWSFeed(asset_ids=["tok_yes_btc_1"])
    for event_type in ("best_bid_ask", "new_market", "market_resolved", "tick_size_change"):
        feed._handle_event({"event_type": event_type, "asset_id": "tok_yes_btc_1"})  # should not raise


def test_price_change_replays_real_captured_shape():
    """
    Mirrors the actual structure captured live from the market channel in
    2026-08: one price_change event carrying a price_changes array with an
    entry per token of the market. Only the entry whose asset_id matches a
    cached book may be applied.
    """
    feed = PolymarketWSFeed(asset_ids=["tok_yes_btc_1"])
    feed._handle_event(load_fixture("sample_ws_book_event.json"))

    feed._handle_event({
        "market": "0x0f8ef3cc906ba7ba94a44724738df44bdd5f73e59e40c9c8b4ff8569e349643c",
        "price_changes": [
            {
                "asset_id": "tok_no_btc_1",  # not cached — must be skipped
                "price": "0.998", "size": "10", "side": "SELL",
                "hash": "0xabc", "best_bid": "0.997", "best_ask": "0.998",
            },
            {
                "asset_id": "tok_yes_btc_1",  # cached — must be applied
                "price": "0.545", "size": "250", "side": "BUY",
                "hash": "0xdef", "best_bid": "0.541", "best_ask": "0.549",
            },
        ],
        "timestamp": "1785683287942",
        "event_type": "price_change",
    })

    book = feed.get_cached_book("tok_yes_btc_1")
    assert book is not None
    assert any(lvl.price == pytest.approx(0.545) for lvl in book.bids)
    assert all(lvl.size == pytest.approx(250.0) for lvl in book.bids if lvl.price == pytest.approx(0.545))


# -- FeedHealth callbacks (on_message / on_reconnect) ---------------------------

class _FakeWS:
    """Fake websocket: yields a scripted list of raw messages, then raises
    ConnectionClosed so run()'s reconnect path (and on_reconnect) executes."""

    def __init__(self, raw_messages: list[bytes]):
        self._messages = list(raw_messages)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, _data):  # _connect() sends the subscribe payload
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        raise websockets.exceptions.ConnectionClosed(None, None)


async def test_run_fires_on_message_callback():
    """Every raw message delivered by run() must invoke on_message."""
    messages = []
    feed = PolymarketWSFeed(
        asset_ids=["tok_yes_btc_1"],
        on_message=lambda: messages.append(1),
    )
    event = load_fixture("sample_ws_book_event.json")
    feed._connect = _fake_connect([json.dumps(event).encode()])  # type: ignore[method-assign]

    task = asyncio.create_task(feed.run())
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert messages  # at least one raw message was delivered
    # And the message was actually handled (book cached).
    assert feed.get_cached_book("tok_yes_btc_1") is not None


async def test_run_fires_on_reconnect_callback_after_drop():
    """A dropped connection must invoke on_reconnect."""
    reconnects = []
    feed = PolymarketWSFeed(
        asset_ids=["tok_yes_btc_1"],
        on_reconnect=lambda: reconnects.append(1),
    )
    feed._connect = _fake_connect([])  # type: ignore[method-assign]  # drops immediately

    task = asyncio.create_task(feed.run())
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert reconnects  # callback fired after the drop


def _fake_connect(raw_messages: list[bytes]):
    """Returns an async connect() replacement returning a scripted _FakeWS.
    Deliberately NOT async itself: feed.run() awaits `self._connect()`, so
    `_connect` must be a callable returning a coroutine, not a coroutine."""

    async def connect():
        return _FakeWS(raw_messages)

    return connect
