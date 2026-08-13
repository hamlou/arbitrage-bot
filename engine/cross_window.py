"""
Cross-window arbitrage (added 2026-08-13): a second risk-free leg, sourced
from public Polymarket-bot research (HorseDev77's 5m/15m same-endTime bot)
and verified mathematically before building — not copied.

The structure: two Polymarket windows on the SAME asset (BTC/ETH) with the
SAME endTime resolve against the SAME end-of-window price, but each has its
OWN beat price — the underlying price at THAT window's open (a 15m window's
beat is the price 15 minutes before end; the 5m window sharing its endTime
has the price 5 minutes before end). When the beats differ (X < Y), buying
UP on the lower-beat window + DOWN on the higher-beat window pays:

    final > Y          -> UP(lower)  wins -> $1
    X < final <= Y     -> BOTH win         -> $2   (the middle band)
    final <= X         -> DOWN(higher) wins -> $1

i.e. at least $1 for every possible resolution, exactly like sum-to-one —
arbitrage by arithmetic, with zero direction prediction. The two legs are
bought in EQUAL share counts (the same equal-share sizing sum-to-one uses,
so the worst case is exactly $1 per pair) and held to settlement.

Unlike sum-to-one this is not restricted to one market's YES+NO; it exploits
the nesting of same-endTime windows. The beat prices are NOT served by
Gamma — the bot approximates them with its first-sighting Binance reference
price, which is why pairing additionally requires the reference to have been
captured within a few seconds of each window's OPEN (CROSS_WINDOW_MAX_REF_CAPTURE_DELAY_S)
and the beats to be separated by a minimum gap: an ordering mistake turns the
guaranteed arb into a directional bet, so the gap must dominate the capture
noise.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from data.polymarket_feed import Market, OrderBook
from engine.fees import fee_rate_for_category, taker_fee_pct


@dataclass(frozen=True, slots=True)
class CrossWindowOpportunity:
    # market_a = lower-beat window, market_b = higher-beat window.
    # We buy UP on the lower-beat window and DOWN on the higher-beat window —
    # the combination guaranteed to pay >= $1 at settlement.
    market_a: Market
    market_b: Market
    side_a: str            # "YES" (UP) on the lower-beat window
    side_b: str            # "NO"  (DOWN) on the higher-beat window
    token_id_a: str
    token_id_b: str
    beat_gap_pct: float    # |beat_b - beat_a| / beat_a (fraction)
    combined_cost: float   # ask_a + ask_b, before fees
    net_profit_pct: float  # guaranteed profit per dollar staked, AFTER modeled fees


def cross_window_payout(final_price: float, lower_beat: float, higher_beat: float) -> float:
    """Payout (per equal-share pair) of the cross-window arb at a given final
    underlying price: $1 in the outer bands, $2 in the middle band. Pure
    payoff math, exposed for tests — the arb is valid iff this is >= 1 for
    every final price when beats are ordered lower < higher."""
    if final_price > higher_beat:
        return 1.0   # only UP(lower beat) wins
    if final_price > lower_beat:
        return 2.0   # both legs win (middle band)
    return 1.0       # only DOWN(higher beat) wins


def find_cross_window_pair(
    group: list[Market],
    *,
    max_ref_capture_delay_s: float,
    min_beat_gap_pct: float,
    now: Optional[float] = None,
) -> Optional[tuple[Market, Market]]:
    """
    Among windows sharing one (asset, endTime), pick the trusted
    lower-beat / higher-beat pair, or None.

    Trust requirements (both windows):
      - 5m or 15m duration (the only durations this arb class exists on),
      - a reference price exists AND its capture time is known,
      - the reference was captured within max_ref_capture_delay_s of the
        window's OPEN (open == expires - duration) — i.e. the first-sighting
        approximation is within a few seconds of the real beat price,
      - the two beats differ by at least min_beat_gap_pct (relative), so the
        ordering decision is robust to the approximation noise.

    Returns (lower_beat_market, higher_beat_market).
    """
    now = time.time() if now is None else now
    trusted: dict[int, Market] = {}
    for m in group:
        d = m.duration_minutes
        if d not in (5, 15):
            continue
        if m.reference_price is None or m.reference_captured_at is None:
            continue
        if m.expires_at_ts is None:
            continue
        open_ts = m.expires_at_ts - d * 60
        if m.reference_captured_at - open_ts > max_ref_capture_delay_s:
            continue
        trusted[d] = m
    if 5 not in trusted or 15 not in trusted:
        return None
    m5, m15 = trusted[5], trusted[15]
    ref5: float = m5.reference_price  # type: ignore[assignment]
    ref15: float = m15.reference_price  # type: ignore[assignment]
    gap = abs(ref5 - ref15) / min(ref5, ref15)
    if gap < min_beat_gap_pct:
        return None
    if ref5 < ref15:
        return m5, m15
    return m15, m5


def find_cross_window_opportunity(
    lower: Market,
    higher: Market,
    lower_up_book: OrderBook,      # YES book of the lower-beat window
    higher_down_book: OrderBook,   # NO book of the higher-beat window
    min_edge_pct: float,
    fee_pct: float,
) -> Optional[CrossWindowOpportunity]:
    """
    Detect a tradable cross-window arb: buy UP on the lower-beat window and
    DOWN on the higher-beat window (same endTime), holding both to
    settlement. If the two asks (net of modeled fees) sum below $1, the pair
    is guaranteed >= $1 regardless of outcome — the same arithmetic as
    sum-to-one. Returns None when no edge clears min_edge_pct.
    """
    ask_lower = lower_up_book.best_ask
    ask_higher = higher_down_book.best_ask
    if ask_lower is None or ask_higher is None:
        return None

    combined_cost = ask_lower + ask_higher
    if combined_cost <= 0:
        return None

    profit_before_fees = 1.0 - combined_cost
    # Category-aware fee RATE per window (both are crypto up/down in
    # practice -> 0.07; the fee-free geopolitics case is irrelevant here but
    # the helper keeps the same honesty as sum-to-one).
    rate_lower = fee_rate_for_category(lower.category) if lower.category else fee_pct
    rate_higher = fee_rate_for_category(higher.category) if higher.category else fee_pct
    fee_cost = taker_fee_pct(ask_lower, rate_lower) + taker_fee_pct(ask_higher, rate_higher)
    net_profit_pct = (profit_before_fees - fee_cost) / combined_cost

    if net_profit_pct <= min_edge_pct:
        return None

    lower_beat = lower.reference_price or 0.0
    higher_beat = higher.reference_price or 0.0
    beat_gap_pct = abs(higher_beat - lower_beat) / lower_beat if lower_beat else 0.0

    return CrossWindowOpportunity(
        market_a=lower, market_b=higher,
        side_a="YES", side_b="NO",
        token_id_a=lower.token_id_yes, token_id_b=higher.token_id_no,
        beat_gap_pct=beat_gap_pct,
        combined_cost=combined_cost, net_profit_pct=net_profit_pct,
    )
