"""
Real-time Polymarket order-book feed via the CLOB's public market-data
WebSocket channel — replaces 1s REST polling with push updates.

Endpoint, subscribe payload, and event schema verified against
https://docs.polymarket.com/market-data/websocket/market-channel (confirmed
unchanged by the 2026-04-28 CLOB V2 migration notes: "WebSocket URLs are
unchanged and message payloads are mostly unchanged").

    wss://ws-subscriptions-clob.polymarket.com/ws/market

Subscribe:
    {"assets_ids": ["<token_id_1>", "<token_id_2>", ...], "type": "market",
     "custom_feature_enabled": true}

This is public market data — no credentials required. Read-only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

import websockets
import websockets.exceptions
from tenacity import retry, retry_if_exception_type, stop_never, wait_exponential_jitter

from data.polymarket_feed import OrderBook, OrderBookLevel

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL_S = 20
PING_TIMEOUT_S = 10
STALE_AFTER_S = 5.0  # if we haven't heard from the WS in this long, treat the cache as unreliable


def _parse_levels(raw_levels: list[dict]) -> tuple[OrderBookLevel, ...]:
    return tuple(
        OrderBookLevel(price=float(lvl["price"]), size=float(lvl["size"]))
        for lvl in raw_levels
    )


def _close_code_and_reason(exc: "websockets.exceptions.ConnectionClosed"):
    """Best-effort close code/reason from a ConnectionClosed, via the rcvd/sent
    frames (the .code/.reason properties are deprecated in websockets >= 13).
    Returns (code, reason) where either may be None."""
    for attr in ("rcvd", "sent"):
        frame = getattr(exc, attr, None)
        if frame is not None:
            return getattr(frame, "code", None), getattr(frame, "reason", None)
    return None, None


class PolymarketWSFeed:
    """
    Subscribes to a set of token IDs and maintains an in-memory, continuously
    updated OrderBook cache per token. `get_cached_book()` is O(1) and doesn't
    touch the network — this is the whole point versus REST polling.
    """

    def __init__(
        self,
        asset_ids: list[str],
        on_message: Optional[Callable[[], None]] = None,
        on_reconnect: Optional[Callable[[], None]] = None,
    ):
        self.asset_ids = list(asset_ids)
        self._books: dict[str, OrderBook] = {}
        self._last_update_at: dict[str, float] = {}
        self._connected = asyncio.Event()
        self._needs_resubscribe = asyncio.Event()
        self._current_ws = None
        # Health hooks: on_message fires once per raw WS message received;
        # on_reconnect fires every time the connection is re-established after
        # a drop. Both are consumed by engine/feed_health.FeedHealth in main.py.
        self.on_message = on_message
        self.on_reconnect = on_reconnect

    def get_cached_book(self, token_id: str) -> Optional[OrderBook]:
        """Returns the most recent book for token_id, or None if we've never seen one."""
        return self._books.get(token_id)

    def is_fresh(self, token_id: str, max_age_s: float = STALE_AFTER_S) -> bool:
        last = self._last_update_at.get(token_id)
        if last is None:
            return False
        return (time.time() - last) <= max_age_s

    def update_assets(self, asset_ids: list[str]) -> None:
        """
        Replace the subscribed asset set (e.g. as new 5/15-min markets roll
        in and old ones expire). Markets rotate every few minutes, so this
        needs to be called periodically by the caller (see main.py) — the WS
        connection itself doesn't know when new markets appear.

        Implementation note: rather than guessing whether the market channel
        supports additive subscription on an already-open connection (not
        independently confirmed here), this forces a clean reconnect with the
        full updated asset list. Slightly more overhead than an incremental
        subscribe, but guaranteed correct.
        """
        new_set = set(asset_ids)
        if new_set != set(self.asset_ids):
            self.asset_ids = list(asset_ids)
            self._needs_resubscribe.set()

    def _handle_event(self, event: dict) -> None:
        event_type = event.get("event_type")

        if event_type == "book":
            asset_id = event.get("asset_id")
            if not asset_id:
                return
            try:
                book = OrderBook(
                    market_id=event.get("market", ""),
                    token_id=asset_id,
                    bids=_parse_levels(event.get("bids", [])),
                    asks=_parse_levels(event.get("asks", [])),
                )
            except (KeyError, ValueError, TypeError):
                logger.debug("Malformed 'book' event, skipping: %s", event)
                return
            self._books[asset_id] = book
            self._last_update_at[asset_id] = time.time()

        elif event_type == "price_change":
            # Verified against a live capture (2026-08-02): a price_change
            # event wraps a "price_changes" ARRAY; each entry carries its own
            # asset_id plus price/size/side (side arrives UPPERCASE "BUY"/
            # "SELL"). Incremental single-level updates — only apply to
            # assets we already hold a base book for; otherwise wait for the
            # next full 'book' snapshot.
            for change in event.get("price_changes", []):
                if not isinstance(change, dict):
                    continue
                asset_id = change.get("asset_id")
                if not asset_id or asset_id not in self._books:
                    continue
                self._apply_price_change(asset_id, change)
                self._last_update_at[asset_id] = time.time()

        elif event_type in ("best_bid_ask", "new_market", "market_resolved", "tick_size_change"):
            # Not needed for order-book depth; log at debug for visibility.
            logger.debug("Unhandled event_type=%s: %s", event_type, event)

    def _apply_price_change(self, asset_id: str, change: dict) -> None:
        """
        Merge a single price_change entry (one element of the real
        price_changes[] array) into the cached book for asset_id. Each entry
        carries a `price`, `size`, and `side` (uppercase "BUY"/"SELL" in
        practice; lowercased before comparing). A size of "0" means that level
        should be removed from the book.
        """
        book = self._books[asset_id]
        price = change.get("price")
        size = change.get("size")
        side = (change.get("side") or "").lower()
        if price is None or size is None or side not in ("buy", "sell"):
            return

        price_f, size_f = float(price), float(size)
        levels = list(book.bids if side == "buy" else book.asks)

        levels = [lvl for lvl in levels if lvl.price != price_f]
        if size_f > 0:
            levels.append(OrderBookLevel(price=price_f, size=size_f))

        if side == "buy":
            levels.sort(key=lambda lvl: -lvl.price)
            new_book = OrderBook(market_id=book.market_id, token_id=asset_id, bids=tuple(levels), asks=book.asks)
        else:
            levels.sort(key=lambda lvl: lvl.price)
            new_book = OrderBook(market_id=book.market_id, token_id=asset_id, bids=book.bids, asks=tuple(levels))

        self._books[asset_id] = new_book

    @retry(
        retry=retry_if_exception_type(
            (websockets.exceptions.ConnectionClosed, websockets.exceptions.WebSocketException, OSError)
        ),
        wait=wait_exponential_jitter(initial=1, max=60),
        stop=stop_never,
        reraise=False,
    )
    async def _connect(self):
        logger.info("Connecting to Polymarket market-data WS for %d assets", len(self.asset_ids))
        ws = await websockets.connect(WS_URL, ping_interval=PING_INTERVAL_S, ping_timeout=PING_TIMEOUT_S)
        subscribe_msg = {
            "assets_ids": self.asset_ids,
            "type": "market",
            "custom_feature_enabled": True,
        }
        await ws.send(json.dumps(subscribe_msg))
        return ws

    async def run(self) -> None:
        """
        Long-running coroutine: connects, subscribes, and keeps the book cache
        updated forever, reconnecting with backoff on any drop. Intended to be
        launched as its own asyncio.Task (see main.py).
        """
        while True:
            if not self.asset_ids:
                await asyncio.sleep(1)
                continue
            try:
                ws = await self._connect()
            except Exception:
                logger.exception("Failed to connect to Polymarket WS, retrying")
                if self.on_reconnect is not None:
                    self.on_reconnect()
                await asyncio.sleep(1)
                continue

            self._connected.set()
            self._current_ws = ws
            self._needs_resubscribe.clear()
            try:
                async with ws:
                    async for raw_msg in ws:
                        # Any raw message (even a malformed one) counts as
                        # liveness — the socket is delivering data.
                        if self.on_message is not None:
                            self.on_message()
                        if self._needs_resubscribe.is_set():
                            logger.info("Asset list changed, reconnecting to resubscribe")
                            break
                        try:
                            payload = json.loads(raw_msg)
                        except json.JSONDecodeError:
                            continue
                        # The market channel can send a single event dict or a list of them.
                        events = payload if isinstance(payload, list) else [payload]
                        for event in events:
                            if isinstance(event, dict):
                                self._handle_event(event)
            except websockets.exceptions.ConnectionClosed as exc:
                # Log WHY it dropped — the close code separates ISP-level
                # flapping (1006 abnormal closure / ping timeouts) from
                # server-side closes (1000/1001), so reconnect storms are
                # diagnosable instead of just visible as a count. Access via
                # the rcvd/sent frames (the .code/.reason properties are
                # deprecated in websockets >= 13).
                code, reason = _close_code_and_reason(exc)
                logger.warning(
                    "Polymarket WS connection dropped: code=%s reason=%r — reconnecting",
                    code, reason,
                )
                self._connected.clear()
                if self.on_reconnect is not None:
                    self.on_reconnect()
                await asyncio.sleep(1)
                continue
            except (websockets.exceptions.WebSocketException, OSError) as exc:
                logger.warning("Polymarket WS connection dropped: %r — reconnecting", exc)
                self._connected.clear()
                if self.on_reconnect is not None:
                    self.on_reconnect()
                await asyncio.sleep(1)
                continue
            else:
                # Loop exited cleanly (either resubscribe requested or the
                # server closed the iterator) — fall through to reconnect.
                self._connected.clear()
                continue

    async def wait_connected(self, timeout: float = 10.0) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
