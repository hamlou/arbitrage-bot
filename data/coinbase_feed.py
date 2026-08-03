"""
Public Coinbase market-data feed. No API key required — this connects to
Coinbase Exchange's public WebSocket (wss://ws-feed.exchange.coinbase.com)
for ticker + match (trade) data on BTC-USD / ETH-USD.

This mirrors the structure of data/binance_feed.py: the same PriceUpdate
dataclass, the same _parse_message-style normalization, the same
CoinbaseFeed.stream() async-generator with automatic reconnect via tenacity,
and the same module-level stream() convenience wrapper.

Protocol facts (verified against Coinbase's public WS docs):
  - Single endpoint; subscribe with:
        {"type": "subscribe", "product_ids": ["BTC-USD", "ETH-USD"],
         "channels": ["ticker", "matches"]}
  - "ticker" channel: {"type": "ticker", "product_id": "BTC-USD",
                       "price": "8700.13", "time": "2019-11-14T20:52:27.452044Z",
                       "side": "buy", "last_size": "...", ...}
  - "matches" channel (trades): {"type": "match", "product_id": "BTC-USD",
                                 "price": "400.00", "size": "1.2356",
                                 "time": "2014-11-07T08:19:27.028459Z",
                                 "side": "buy", ...}
  - The server also sends "subscriptions" (subscription confirmation) and,
    if subscribed, "heartbeat" messages — both are ignored here (no PriceUpdate).

Symbol convention: the engine (engine/signal.py) keys its momentum trackers by
f"{asset}USDT" (e.g. "BTCUSDT"). Coinbase's product IDs are "BTC-USD", so this
feed normalizes product_id -> the engine's symbol on the way in ("BTC-USD" ->
"BTCUSDT"), making it a drop-in replacement for BinanceFeed.

This module reads market data only. It never places orders and never touches
a wallet of any kind.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Iterable

import websockets
import websockets.exceptions
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_never,
    wait_exponential_jitter,
)

logger = logging.getLogger(__name__)

COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"
DEFAULT_PRODUCTS = ("BTC-USD", "ETH-USD")
PING_INTERVAL_S = 20
PING_TIMEOUT_S = 10

# Map Coinbase product IDs to the engine's symbol convention (asset + "USDT").
_PRODUCT_TO_SYMBOL = {
    "BTC-USD": "BTCUSDT",
    "ETH-USD": "ETHUSDT",
}


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """A single normalized price tick from Coinbase."""

    symbol: str          # e.g. "BTCUSDT" (normalized from Coinbase's "BTC-USD")
    price: float
    event_time_ms: int   # exchange-reported event time (epoch ms)
    received_at: float   # local wall clock time.time()
    kind: str            # "trade" (match) or "ticker"

    @property
    def age_seconds(self) -> float:
        return time.time() - self.received_at


def _normalize_symbol(product_id: str) -> str:
    """Map a Coinbase product id (\"BTC-USD\") to the engine's symbol (\"BTCUSDT\").

    Unmapped products pass through unchanged (e.g. \"SOL-USD\"). Callers should
    log at warning when that happens: the engine keys its trackers by
    f\"{asset}USDT\", so a passthrough symbol will never be looked up.
    """
    return _PRODUCT_TO_SYMBOL.get(product_id, product_id)


def _iso_to_epoch_ms(iso: str) -> int | None:
    """
    Parse a Coinbase ISO-8601 timestamp (e.g. \"2019-11-14T20:52:27.452044Z\")
    into epoch milliseconds. Assumes UTC when no explicit offset is present.
    Returns None (not 0) for empty or unparseable input so callers can tell
    "unknown event time" apart from a real epoch-0 timestamp.
    """
    if not iso:
        return None
    normalized = iso[:-1] + "+00:00" if iso.endswith("Z") else iso
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _build_subscribe_message(product_ids: Iterable[str]) -> dict:
    return {
        "type": "subscribe",
        "product_ids": list(product_ids),
        "channels": ["ticker", "matches"],
    }


def _parse_message(raw: dict) -> PriceUpdate | None:
    """Normalize a raw Coinbase WS payload into a PriceUpdate (or None if it's
    not a price-carrying message or is malformed). Requires the same strict
    fields Binance's feed requires: product_id, price, and a parseable time —
    a message missing any of them is dropped, never half-parsed."""
    if not isinstance(raw, dict):
        return None  # defensive: Coinbase always sends objects, but never trust the wire
    msg_type = raw.get("type")
    if msg_type not in ("ticker", "match"):
        return None  # subscriptions confirmation, heartbeat, etc.

    now = time.time()
    try:
        price = float(raw["price"])
        product_id = str(raw["product_id"]).strip()
        time_str = str(raw["time"]).strip()
    except (KeyError, ValueError, TypeError):
        logger.debug("Malformed %s payload (missing fields): %s", msg_type, raw)
        return None
    if not product_id or not time_str:
        logger.debug("Malformed %s payload (empty product_id/time): %s", msg_type, raw)
        return None

    event_time_ms = _iso_to_epoch_ms(time_str)
    if event_time_ms is None:
        logger.debug("Unparseable timestamp in %s payload: %s", msg_type, raw)
        return None

    symbol = _normalize_symbol(product_id)
    if symbol == product_id:
        # Unmapped product: the engine keys trackers by f"{asset}USDT", so a
        # passthrough symbol like "SOL-USD" will never be looked up there.
        # Yield the tick (price data is still valid) but make the gap visible.
        logger.warning(
            "Coinbase product %r has no engine-symbol mapping — its ticks will "
            "not feed the momentum engine", product_id
        )

    return PriceUpdate(
        symbol=symbol,
        price=price,
        event_time_ms=event_time_ms,
        received_at=now,
        kind="trade" if msg_type == "match" else "ticker",
    )


class CoinbaseFeed:
    """
    Async client for Coinbase's public market-data WebSocket.

    Usage:
        feed = CoinbaseFeed(products=["BTC-USD", "ETH-USD"])
        async for update in feed.stream():
            ...
    """

    def __init__(self, products: Iterable[str] = DEFAULT_PRODUCTS):
        self.products = tuple(products)
        self._subscribe_msg = _build_subscribe_message(self.products)

    @retry(
        retry=retry_if_exception_type(
            (
                websockets.exceptions.ConnectionClosed,
                websockets.exceptions.WebSocketException,
                OSError,
                asyncio.TimeoutError,
            )
        ),
        wait=wait_exponential_jitter(initial=1, max=60),
        stop=stop_never,
        reraise=False,
    )
    async def _connect(self):
        logger.info("Connecting to Coinbase WS: %s", COINBASE_WS_URL)
        ws = await websockets.connect(
            COINBASE_WS_URL,
            ping_interval=PING_INTERVAL_S,
            ping_timeout=PING_TIMEOUT_S,
            close_timeout=5,
        )
        await ws.send(json.dumps(self._subscribe_msg))
        return ws

    async def stream(self) -> AsyncIterator[PriceUpdate]:
        """
        Async generator yielding PriceUpdate objects forever. Reconnects
        automatically (exponential backoff with jitter) on any connection
        error; the generator itself never raises for transient network issues.
        """
        while True:
            try:
                ws = await self._connect()
            except Exception:  # pragma: no cover - defensive, retry() should absorb this
                logger.exception("Failed to establish Coinbase WS connection, retrying")
                await asyncio.sleep(1)
                continue

            try:
                async with ws:
                    async for raw_msg in ws:
                        try:
                            payload = json.loads(raw_msg)
                        except json.JSONDecodeError:
                            logger.debug("Non-JSON message from Coinbase, skipping")
                            continue
                        update = _parse_message(payload)
                        if update is not None:
                            yield update
            except (
                websockets.exceptions.ConnectionClosed,
                websockets.exceptions.WebSocketException,
                OSError,
            ):
                logger.warning("Coinbase WS connection dropped, reconnecting")
                await asyncio.sleep(1)
                continue


async def stream(products: Iterable[str] = DEFAULT_PRODUCTS) -> AsyncIterator[PriceUpdate]:
    """Module-level convenience wrapper matching binance_feed.stream()."""
    feed = CoinbaseFeed(products=products)
    async for update in feed.stream():
        yield update
