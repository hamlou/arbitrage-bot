"""
Public Binance market-data feed. No API key required — this hits Binance's
public combined-stream WebSocket for trade + ticker data on BTCUSDT / ETHUSDT.

This module reads market data only. It never places orders and never touches
a wallet of any kind.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Iterable, Optional

import websockets
import websockets.exceptions
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_never,
    wait_exponential_jitter,
)

logger = logging.getLogger(__name__)

BINANCE_WS_BASE = "wss://stream.binance.com:9443/stream"
DEFAULT_SYMBOLS = ("btcusdt", "ethusdt")
PING_INTERVAL_S = 20
PING_TIMEOUT_S = 10


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """A single normalized price tick from Binance."""

    symbol: str          # e.g. "BTCUSDT"
    price: float
    event_time_ms: int   # exchange-reported event time
    received_at: float   # local monotonic-ish wall clock time.time()
    kind: str            # "trade" or "ticker"

    @property
    def age_seconds(self) -> float:
        return time.time() - self.received_at


def _build_stream_url(symbols: Iterable[str]) -> str:
    streams = []
    for sym in symbols:
        sym = sym.lower()
        streams.append(f"{sym}@trade")
        streams.append(f"{sym}@ticker")
    return f"{BINANCE_WS_BASE}?streams={'/'.join(streams)}"


def _parse_message(raw: dict) -> PriceUpdate | None:
    """Normalize a raw Binance combined-stream payload into a PriceUpdate."""
    data = raw.get("data")
    stream = raw.get("stream", "")
    if not data:
        return None

    now = time.time()

    if stream.endswith("@trade"):
        # {"e":"trade","E":<ms>,"s":"BTCUSDT","p":"67123.45", ...}
        try:
            return PriceUpdate(
                symbol=data["s"],
                price=float(data["p"]),
                event_time_ms=int(data["E"]),
                received_at=now,
                kind="trade",
            )
        except (KeyError, ValueError, TypeError):
            logger.debug("Malformed trade payload: %s", data)
            return None

    if stream.endswith("@ticker"):
        # {"e":"24hrTicker","E":<ms>,"s":"BTCUSDT","c":"67123.45", ...}
        try:
            return PriceUpdate(
                symbol=data["s"],
                price=float(data["c"]),
                event_time_ms=int(data["E"]),
                received_at=now,
                kind="ticker",
            )
        except (KeyError, ValueError, TypeError):
            logger.debug("Malformed ticker payload: %s", data)
            return None

    return None


class BinanceFeed:
    """
    Async client for Binance's public combined WebSocket stream.

    Usage:
        feed = BinanceFeed(symbols=["btcusdt", "ethusdt"])
        async for update in feed.stream():
            ...
    """

    def __init__(
        self,
        symbols: Iterable[str] = DEFAULT_SYMBOLS,
        on_reconnect: Optional[Callable[[], None]] = None,
    ):
        self.symbols = tuple(symbols)
        self._url = _build_stream_url(self.symbols)
        # Called every time the connection is re-established after a drop.
        self.on_reconnect = on_reconnect

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
        logger.info("Connecting to Binance WS: %s", self._url)
        return await websockets.connect(
            self._url,
            ping_interval=PING_INTERVAL_S,
            ping_timeout=PING_TIMEOUT_S,
            close_timeout=5,
        )

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
                logger.exception("Failed to establish Binance WS connection, retrying")
                if self.on_reconnect is not None:
                    self.on_reconnect()
                await asyncio.sleep(1)
                continue

            try:
                async with ws:
                    async for raw_msg in ws:
                        try:
                            payload = json.loads(raw_msg)
                        except json.JSONDecodeError:
                            logger.debug("Non-JSON message from Binance, skipping")
                            continue
                        update = _parse_message(payload)
                        if update is not None:
                            yield update
            except (
                websockets.exceptions.ConnectionClosed,
                websockets.exceptions.WebSocketException,
                OSError,
            ):
                logger.warning("Binance WS connection dropped, reconnecting")
                if self.on_reconnect is not None:
                    self.on_reconnect()
                await asyncio.sleep(1)
                continue


async def stream(symbols: Iterable[str] = DEFAULT_SYMBOLS) -> AsyncIterator[PriceUpdate]:
    """Module-level convenience wrapper matching the spec's requested interface."""
    feed = BinanceFeed(symbols=symbols)
    async for update in feed.stream():
        yield update
