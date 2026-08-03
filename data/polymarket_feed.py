"""
Public, read-only Polymarket data access.

- Gamma API (gamma-api.polymarket.com): market discovery/metadata.
- CLOB API (clob.polymarket.com) read-only endpoints: live order-book depth.

This module requires zero credentials. It never signs or submits an order.
Note the deliberate choice to read order-book depth from the CLOB rather than
trusting Gamma's `outcomePrices` alone — Gamma can lag the live book by a few
seconds, and the whole strategy in this project depends on not adding
avoidable staleness on our own end.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace as dataclass_replace
from datetime import datetime
from typing import Any, Optional

import httpx  # lightweight, already a transitive dep of most web stacks; add to requirements if not present
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

DEFAULT_POLL_INTERVAL_S = 1.0


@dataclass(frozen=True, slots=True)
class OrderBookLevel:
    price: float
    size: float


@dataclass(frozen=True, slots=True)
class OrderBook:
    market_id: str
    token_id: str
    bids: tuple[OrderBookLevel, ...]  # best-first (highest price first)
    asks: tuple[OrderBookLevel, ...]  # best-first (lowest price first)
    fetched_at: float = field(default_factory=time.time)

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    def depth_usd(self, side: str, levels: int = 5) -> float:
        """Approximate USD depth available on one side within `levels` price levels."""
        book_side = self.bids if side == "bid" else self.asks
        return sum(lvl.price * lvl.size for lvl in book_side[:levels])


@dataclass(frozen=True, slots=True)
class Market:
    market_id: str
    question: str
    token_id_yes: str
    token_id_no: str
    liquidity_usd: float
    end_date_iso: str
    asset: str          # "BTC" or "ETH"
    duration_minutes: int  # 5 or 15
    resolved: bool = False
    outcome: Optional[str] = None  # set only once resolved
    # The price the underlying asset must beat for YES to win. NOT reliably
    # available from Gamma's public schema (unverified against a confirmed
    # field name) — populated as a practical fallback: the Binance price
    # observed at the moment this market was FIRST seen by our own discovery
    # loop (see main.py's market-discovery task). This is an approximation,
    # not the platform's authoritative reference price — if the bot starts
    # mid-contract, or discovery lags a market's true open, this will be
    # wrong. Flagged clearly rather than silently assumed correct.
    reference_price: Optional[float] = None
    expires_at_ts: Optional[float] = None  # parsed from end_date_iso

    def with_reference_price(self, price: float) -> "Market":
        """Frozen dataclass — returns a copy with reference_price set."""
        return dataclass_replace(self, reference_price=price)

    @property
    def time_remaining_s(self) -> Optional[float]:
        if self.expires_at_ts is None:
            return None
        return max(0.0, self.expires_at_ts - time.time())


class PolymarketFeed:
    """
    Discovers active short-duration BTC/ETH markets via Gamma, then polls
    live order-book depth via the CLOB. Zero credentials required.
    """

    def __init__(
        self,
        min_liquidity_usd: float,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        client: Optional[httpx.AsyncClient] = None,
        ws_feed: Optional[Any] = None,
    ):
        self.min_liquidity_usd = min_liquidity_usd
        self.poll_interval_s = poll_interval_s
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._owns_client = client is None
        # Optional PolymarketWSFeed instance (see data/polymarket_ws_feed.py).
        # When set and its cache is fresh, get_order_book() uses the pushed
        # book instead of making a REST round-trip — this is the difference
        # between reacting to a several-second-old snapshot and a live one.
        self._ws_feed = ws_feed

    async def aclose(self):
        if self._owns_client:
            await self._client.aclose()

    # -- Gamma: market discovery -------------------------------------------------

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        wait=wait_exponential_jitter(initial=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def _gamma_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._client.get(f"{GAMMA_BASE}{path}", params=params)
        if resp.status_code == 429:
            # Explicit backoff signal from the rate limiter.
            raise httpx.HTTPStatusError("rate limited", request=resp.request, response=resp)
        resp.raise_for_status()
        return resp.json()

    async def discover_active_markets(self) -> list[Market]:
        """
        Pull active BTC/ETH 5-minute and 15-minute up/down markets from Gamma,
        filtered to those meeting min_liquidity_usd.
        """
        raw = await self._gamma_get(
            "/markets",
            params={"active": "true", "closed": "false", "limit": 200},
        )
        markets: list[Market] = []
        for m in raw:
            parsed = _parse_gamma_market(m)
            if parsed is None:
                continue
            if parsed.liquidity_usd < self.min_liquidity_usd:
                continue
            markets.append(parsed)
        return markets

    async def get_market_outcome(self, market_id: str) -> Optional[str]:
        """After expiry, fetch the resolved outcome for settlement purposes."""
        raw = await self._gamma_get(f"/markets/{market_id}")
        if not raw or not raw.get("closed"):
            return None
        return raw.get("outcome") or raw.get("resolvedOutcome")

    async def get_market_by_id(self, market_id: str) -> Optional[Market]:
        """
        Fetches a single market's CURRENT state directly by ID, regardless of
        whether it's still active/open. This exists specifically because
        discover_active_markets() filters to active=true, closed=false — a
        market with an open position that has since resolved will have
        dropped out of that list entirely, so settlement logic must never
        rely on finding it there again. Poll open positions' markets via this
        method instead.
        """
        raw = await self._gamma_get(f"/markets/{market_id}")
        if not raw:
            return None
        return _parse_gamma_market(raw)

    # -- CLOB: live order book depth -----------------------------------------

    async def get_order_book(self, market_id: str, token_id: str) -> OrderBook:
        if self._ws_feed is not None and self._ws_feed.is_fresh(token_id):
            cached = self._ws_feed.get_cached_book(token_id)
            if cached is not None:
                return cached
        return await self._get_order_book_rest(market_id, token_id)

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        wait=wait_exponential_jitter(initial=0.5, max=15),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def _get_order_book_rest(self, market_id: str, token_id: str) -> OrderBook:
        resp = await self._client.get(f"{CLOB_BASE}/book", params={"token_id": token_id})
        if resp.status_code == 429:
            raise httpx.HTTPStatusError("rate limited", request=resp.request, response=resp)
        resp.raise_for_status()
        raw = resp.json()
        bids = tuple(
            OrderBookLevel(price=float(b["price"]), size=float(b["size"]))
            for b in sorted(raw.get("bids", []), key=lambda x: -float(x["price"]))
        )
        asks = tuple(
            OrderBookLevel(price=float(a["price"]), size=float(a["size"]))
            for a in sorted(raw.get("asks", []), key=lambda x: float(x["price"]))
        )
        return OrderBook(market_id=market_id, token_id=token_id, bids=bids, asks=asks)

    async def implied_probability(self, market_id: str, token_id_yes: str) -> Optional[float]:
        """
        Order-book-derived probability of YES, using the mid price of the live
        book rather than Gamma's (potentially stale) outcomePrices field.
        """
        book = await self.get_order_book(market_id, token_id_yes)
        return book.mid

    # -- Polling loop --------------------------------------------------------

    async def poll_markets(self):
        """
        Async generator yielding the current list of eligible active markets,
        re-discovered every poll_interval_s. Rate-limit-aware via the retry
        decorators above; a persistent failure logs and continues rather than
        crashing the loop.
        """
        while True:
            try:
                markets = await self.discover_active_markets()
                yield markets
            except Exception:
                logger.exception("Failed to discover markets this cycle")
            await asyncio.sleep(self.poll_interval_s)


def _parse_gamma_market(m: dict[str, Any]) -> Optional[Market]:
    """
    Best-effort parse of a Gamma market payload into our Market dataclass.
    Returns None for anything that isn't a short-duration BTC/ETH up/down
    market (Gamma's schema mixes in every market type on the platform).
    """
    question = m.get("question", "")
    q_lower = question.lower()

    asset = None
    if "btc" in q_lower or "bitcoin" in q_lower:
        asset = "BTC"
    elif "eth" in q_lower or "ethereum" in q_lower:
        asset = "ETH"
    if asset is None:
        return None

    # Check 15-minute patterns FIRST: "15 min" contains "5 min" as a substring,
    # so checking 5-minute patterns first would misclassify every 15-minute market.
    if "15 min" in q_lower or "15-minute" in q_lower or "15min" in q_lower:
        duration = 15
    elif "5 min" in q_lower or "5-minute" in q_lower or "5min" in q_lower:
        duration = 5
    else:
        return None

    tokens = m.get("clobTokenIds") or m.get("tokens")
    if not tokens or len(tokens) < 2:
        return None

    try:
        liquidity = float(m.get("liquidity", 0) or 0)
    except (TypeError, ValueError):
        liquidity = 0.0

    end_date_iso = m.get("endDate", "")
    expires_at_ts = None
    if end_date_iso:
        try:
            expires_at_ts = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00")).timestamp()
        except ValueError:
            logger.debug("Could not parse endDate %r for market %s", end_date_iso, m.get("id"))

    return Market(
        market_id=str(m.get("id") or m.get("conditionId") or ""),
        question=question,
        token_id_yes=str(tokens[0]),
        token_id_no=str(tokens[1]),
        liquidity_usd=liquidity,
        end_date_iso=end_date_iso,
        asset=asset,
        duration_minutes=duration,
        resolved=bool(m.get("closed", False)),
        expires_at_ts=expires_at_ts,
    )
