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
import json
import logging
import re
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

# Only up/down windows ending within this horizon are worth discovering: a
# pre-created window ending tomorrow is a real market but has no tradeable
# book yet and no short-horizon arbitrage edge — subscribing to it would only
# waste a WS subscription slot. 5m/15m windows roll every few minutes, so a
# 45-minute lookahead keeps a rolling set of ~6-8 live windows per asset.
DISCOVERY_LOOKAHEAD_S = 45 * 60
# Insurance cap on keyset cursor pagination. Verified live 2026-08-06: the
# /events/keyset API serves inconsistent cached slices — sometimes the live
# windows come first (BTC 5m showing ~$500k liquidity), sometimes they are
# buried behind ~1,400 stale Dec-2025 ghost events still flagged active=true.
# A cap of 5 pages (~1,000 events) let discovery give up before reaching the
# live windows on ghost-first responses, so the bot found zero markets. 15
# pages x 200 = 3,000 events clears the ghost crowd with comfortable margin.
MAX_DISCOVERY_PAGES = 15


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


def _safe_ts(iso_str: str | None) -> float | None:
    """Best-effort ISO-8601 -> epoch, for early-exit pagination logic."""
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


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
        Pull the currently-trading (and soon-expiring) BTC/ETH 5-minute and
        15-minute up/down markets from Gamma's /events/keyset endpoint,
        filtered to those meeting min_liquidity_usd.

        Why /events/keyset and NOT /markets? Verified live 2026-08-04: the
        old /markets list endpoint is deprecated/sunset — the API returns
        `deprecation: true`, `sunset: Fri, 01 May 2026`, and a warning header
        `299 - "use /markets/keyset"` — and now serves stale cached rows
        (Dec-2025 up/down markets still marked active with $0 liquidity), so
        any query against it returns zero real candidates. /events/keyset
        with tag_slug=up-or-down returns the actual current windows (verified
        live: "Bitcoin Up or Down - August 4, 7:05AM-7:10AM ET" appears
        there, ordered by endDate ascending, soonest first).

        The up-or-down tag covers every duration (5m/15m/1h/4h/daily...), so
        on top of the parser we additionally require:
          - the window to be live or about to start: endDate within
            [now - 2 min, now + DISCOVERY_LOOKAHEAD_S]. A pre-created window
            ending tomorrow is a real market but has no tradeable book and no
            short-horizon edge — subscribing to it only wastes the slot.
          - liquidity_usd >= min_liquidity_usd (keeps BTC/ETH, drops thin
            altcoin windows).
        """
        now = time.time()
        horizon_max = now + DISCOVERY_LOOKAHEAD_S
        # Page through the keyset cursor. Ordering is endDate-ascending, so
        # once a page's soonest event ends beyond the lookahead horizon, every
        # later page does too — stop there. The page cap is pure insurance
        # against a pathological response; in practice 1-2 pages cover the
        # live windows.
        events: list[dict] = []
        cursor: str | None = None
        for _ in range(MAX_DISCOVERY_PAGES):
            params: dict[str, Any] = {
                "limit": 200,
                "active": "true",
                "closed": "false",
                "tag_slug": "up-or-down",
                "order": "endDate",
                "ascending": "true",
            }
            if cursor:
                params["cursor"] = cursor
            raw = await self._gamma_get("/events/keyset", params=params)
            page_events = raw.get("events", []) if isinstance(raw, dict) else raw
            events.extend(page_events or [])
            cursor = raw.get("next_cursor") if isinstance(raw, dict) else None
            if not cursor:
                break
            # Stop early once this page is entirely beyond the horizon.
            # Guard against None endDates (in some API slices the event-level
            # endDate is missing — it lives on the nested market instead).
            soonest_end = min(
                (ts for ts in (_safe_ts(e.get("endDate")) for e in page_events or []) if ts is not None),
                default=None,
            )
            if soonest_end is not None and soonest_end > horizon_max:
                break

        markets: list[Market] = []
        for ev in events:
            for m in ev.get("markets") or []:
                parsed = _parse_gamma_market(m)
                if parsed is None:
                    continue
                if parsed.liquidity_usd < self.min_liquidity_usd:
                    continue
                exp = parsed.expires_at_ts
                if exp is None or exp < now - 120 or exp > horizon_max:
                    continue
                markets.append(parsed)
        # Deduplicate by market id (defensive: events can share nested markets).
        seen: set[str] = set()
        uniq: list[Market] = []
        for m in markets:
            if m.market_id in seen:
                continue
            seen.add(m.market_id)
            uniq.append(m)
        return uniq

    async def get_market_outcome(self, market_id: str) -> Optional[str]:
        """
        After expiry, fetch the resolved outcome for settlement purposes.
        Returns "YES" or "NO" (the token side that won), or None if the
        market isn't resolved yet.

        Verified live 2026-08-06: the Gamma market payload no longer carries
        the legacy `outcome`/`resolvedOutcome` string. Resolution is now
        expressed as `outcomes` (e.g. ["Up","Down"]) + `outcomePrices`
        (e.g. ["0","1"]) + `umaResolutionStatus` ("resolved"). The winning
        index in outcomePrices maps to the corresponding token: index 0 is
        the YES token (tokens[0] in _parse_gamma_market), index 1 is NO.
        Without this fix settlement never fired and every position stayed
        OPEN forever, freezing the balance and win-rate stats.
        """
        raw = await self._gamma_get(f"/markets/{market_id}")
        if not raw:
            return None
        # Resolution gate: rely on the authoritative flag first; the legacy
        # `closed` flag also exists but umaResolutionStatus is definitive.
        uma = raw.get("umaResolutionStatus")
        resolved = uma == "resolved" or (uma is None and raw.get("closed"))
        if not resolved:
            return None
        prices = raw.get("outcomePrices")
        # Same shape trap as clobTokenIds (verified live 2026-08-06): Gamma
        # encodes outcomePrices as a JSON STRING, not a list — `isinstance(p,
        # list)` alone silently fails and settlement returns None forever.
        # Normalize both shapes before inspecting.
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except (ValueError, TypeError):
                prices = None
        if isinstance(prices, list) and len(prices) >= 2:
            try:
                if float(prices[0]) >= 0.99:
                    return "YES"
                if float(prices[1]) >= 0.99:
                    return "NO"
            except (TypeError, ValueError):
                pass
        # Last-resort fallback for any payload that still uses the legacy fields.
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


# Matches Polymarket's LIVE up/down title window format, e.g.
# "Ethereum Up or Down - December 19, 11:30AM-11:35AM ET". The old titles
# ("- 5 min - 14:00 ET") carried an explicit duration; the current ones
# encode it as a start-end time window instead. Verified against the live
# Gamma API: the default parser found 0 markets while these window titles
# are the actual short-duration contracts.
_TIME_RANGE_RE = re.compile(
    r"(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)?\s*[-\u2013\u2014]\s*"
    r"(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)?"
)


def _duration_from_time_range(question: str) -> Optional[int]:
    """
    Extract the 5-or-15-minute window from a live up/down title like
    '... December 19, 11:30AM-11:35AM ET'. Returns 5 or 15 when the window
    is exactly that long, else None (so long-dated politics markets and
    malformed windows are still rejected). Handles AM/PM and noon/midnight
    crossing (e.g. 11:55AM-12:05PM is 10 minutes, not a wrap).
    """
    m = _TIME_RANGE_RE.search(question)
    if not m:
        return None

    def to_minutes(h: int, minute: int, ampm: Optional[str]) -> int:
        if ampm:
            if ampm.upper() == "PM" and h < 12:
                h += 12
            if ampm.upper() == "AM" and h == 12:
                h = 0
        return h * 60 + minute

    start = to_minutes(int(m.group(1)), int(m.group(2)), m.group(3))
    end = to_minutes(int(m.group(4)), int(m.group(5)), m.group(6))
    if end < start:
        end += 12 * 60  # crossed noon/midnight, e.g. 11:55AM-12:05PM
    diff = end - start
    return diff if diff in (5, 15) else None


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
        # Live titles carry the duration as a time window, not as text.
        duration = _duration_from_time_range(question)
        if duration is None:
            return None

    # clobTokenIds arrives as a real list in some Gamma responses (the
    # legacy /markets endpoint) but as a JSON-ENCODED STRING in the
    # /events/keyset response (verified live 2026-08-04 — the parsed token
    # came out as '[' + '"', which silently broke both the CLOB book fetch
    # and the WS subscription). Normalize both shapes here.
    tokens = m.get("clobTokenIds") or m.get("tokens")
    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except (ValueError, TypeError):
            tokens = None
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
