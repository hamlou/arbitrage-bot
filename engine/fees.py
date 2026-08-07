"""
Polymarket fee model (single source of truth — docs.polymarket.com/trading/fees).

Polymarket's crypto-market taker fee is NOT a flat fraction of size. It is
price-dependent, per share:

    fee = C * fee_rate * p * (1 - p)

where C is the number of shares, p is the share price, and fee_rate is 0.07
for crypto markets (maker fee is 0).

As a fraction of what you actually spend (C * p), that works out to:

    fee / notional = fee_rate * (1 - p)

i.e. ~3.5% of notional per side at p = 0.50, ~1.5% at p = 0.78, and larger
at low prices. A taker round trip (entry + exit) pays it twice, so the
round-trip fee hurdle at p ~ 0.5 is ~7% of notional BEFORE spread.

Why this matters for this strategy (verified 2026-08-07): the earlier flat
2% fee assumption UNDERSTATED mid-price fees — paper results looked better
than live would be, and the round-trip exit threshold sat right at the real
break-even. With the real formula, the profitable latency-arb trades are the
BIG reprices (the article's example is a 20+ point token move), not the
marginal 3-5% divergences.
"""
from __future__ import annotations

from typing import Optional

# Polymarket's crypto taker fee RATE (docs.polymarket.com/trading/fees).
# Applied as fee_rate * p * (1 - p) per share. Maker fee is 0.
DEFAULT_TAKER_FEE_RATE = 0.07


def taker_fee_per_share(price: float, fee_rate: float = DEFAULT_TAKER_FEE_RATE) -> float:
    """The taker fee per SHARE, in price units: fee_rate * p * (1 - p).
    Buying C shares at price p costs C * taker_fee_per_share(p) in fees.
    This is the unit that compares directly to edge_pct (|model - market|,
    also a per-share price difference) and to per-share combined costs like
    a sum-to-one pair's yes_ask + no_ask.

    Peaks at fee_rate * 0.25 (~1.75c per share at fee_rate=0.07, p=0.50);
    zero at p=0 and p=1.
    """
    p = min(max(price, 0.0), 1.0)
    return fee_rate * p * (1 - p)


def taker_fee_fraction_of_notional(price: float, fee_rate: float = DEFAULT_TAKER_FEE_RATE) -> float:
    """The taker fee as a fraction of the DOLLARS SPENT: fee_rate * (1 - p).
    Buying `size_usd` at price p costs `size_usd * taker_fee_fraction_of_notional(p)`
    in fees (since size = C * p and the per-share fee is rate*p*(1-p), the
    fee is (size/p) * rate*p*(1-p) = size * rate*(1-p)).

    ~3.5% of notional at p=0.50, ~1.5% at p=0.78, ~4.9% at p=0.30 — higher
    at low prices, lower at high prices.
    """
    p = min(max(price, 0.0), 1.0)
    return fee_rate * (1.0 - p)


# Backwards-friendly alias: the fee per share, in price units (the unit the
# signal gate and sum-to-one revalidation compare against edge/cost).
taker_fee_pct = taker_fee_per_share


def round_trip_fee_pct(
    entry_price: float,
    exit_price: Optional[float] = None,
    fee_rate: float = DEFAULT_TAKER_FEE_RATE,
) -> float:
    """Total taker fee for a round trip, in PER-SHARE price units: the entry
    fee plus the exit fee. This is directly comparable to the strategy's
    edge_pct (|model-implied - market|), which is also a per-share price
    difference: net gain per share ≈ edge - round_trip_fee. When exit_price
    is omitted, assumes the exit happens near the entry price (the
    round-trip case)."""
    exit_price = entry_price if exit_price is None else exit_price
    return taker_fee_per_share(entry_price, fee_rate) + taker_fee_per_share(exit_price, fee_rate)
