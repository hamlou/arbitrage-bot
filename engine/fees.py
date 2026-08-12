"""
Polymarket fee model (single source of truth).

Polymarket's taker fee is NOT a flat fraction of size. It is price-dependent,
per share:

    fee = C * fee_rate * p * (1 - p)

where C is the number of shares, p is the share price, and fee_rate is the
taker fee coefficient (maker receives a rebate).

As a fraction of what you actually spend (C * p), that works out to:

    fee / notional = fee_rate * (1 - p)

i.e. ~3.5% of notional per side at p = 0.50 (crypto rate 0.07), ~1.5% at
p = 0.78, and larger at low prices. A taker round trip (entry + exit) pays
it twice, so the round-trip fee hurdle at p ~ 0.5 is ~7% of notional BEFORE
spread. The rate is CATEGORY-dependent (crypto 0.07 … geopolitics free) —
see fee_rate_for_category.

Why this matters for this strategy (verified 2026-08-07): the earlier flat
2% fee assumption UNDERSTATED mid-price fees — paper results looked better
than live would be, and the round-trip exit threshold sat right at the real
break-even. With the real formula, the profitable latency-arb trades are the
BIG reprices (the article's example is a 20+ point token move), not the
marginal 3-5% divergences.
"""
from __future__ import annotations

from typing import Optional

# Polymarket's crypto taker fee RATE (fee coefficient Theta). Confirmed
# 2026-08-12 against docs.polymarket.com/trading/fees (authoritative, live):
# "Crypto: taker fee rate 0.07", fee = rate * p * (1 - p) per share, makers
# pay zero and earn a rebate. The earlier 0.06 estimate (2026-08-08, from a
# period when the authoritative page was unreachable) was stale — the docs
# now load and the crypto rate is 0.07. NOTE: the rate is CATEGORY-dependent
# (see fee_rate_for_category below) — crypto is the most expensive at 0.07
# while geopolitics is fee-free. Applied as fee_rate * p * (1 - p) per share.
DEFAULT_TAKER_FEE_RATE = 0.07

# --- Category-aware fee rates (docs.polymarket.com/trading/fees, 2026-08-12) --
# The taker fee rate differs by market category. Geopolitics is fee-FREE;
# crypto is the most expensive (the dynamic fee model introduced Jan 2026
# specifically to curb latency arbitrage on the short-duration crypto
# markets). Makers pay 0 everywhere and earn a rebate (20% on crypto).
CATEGORY_FEE_RATES: dict[str, float] = {
    "crypto": 0.07,
    "sports": 0.05,
    "finance": 0.04,
    "politics": 0.04,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "other": 0.05,
    "mentions": 0.04,
    "tech": 0.04,
    "geopolitics": 0.0,
}

# Gamma exposes the category as a TAG label on the event payload, not as a
# dedicated field (verified live 2026-08-12: event["category"] is None;
# labels like "Politics", "Geopolitics", "Sports" arrive in tags[]). Map
# tag labels -> category key.
_TAG_TO_CATEGORY: dict[str, str] = {
    "crypto": "crypto",
    "sports": "sports",
    "finance": "finance",
    "financial": "finance",
    "politics": "politics",
    "economics": "economics",
    "economy": "economics",
    "culture": "culture",
    "weather": "weather",
    "mentions": "mentions",
    "tech": "tech",
    "technology": "tech",
    "geopolitics": "geopolitics",
}


def fee_rate_for_category(category: Optional[str]) -> float:
    """Taker fee RATE for a market's category/tag label, per the official
    fee schedule. Returns the "other/general" rate (0.05) for unknown or
    missing categories — deliberately NOT the crypto rate: the whole point
    of category-awareness is that only crypto pays 0.07 while geopolitics is
    free and politics/finance are 0.04. The short-duration BTC/ETH up/down
    markets are category "crypto" (0.07), which is why DEFAULT_TAKER_FEE_RATE
    matches."""
    if not category:
        return CATEGORY_FEE_RATES["other"]
    key = _TAG_TO_CATEGORY.get(category.strip().lower(), "other")
    return CATEGORY_FEE_RATES[key]


def taker_fee_per_share(price: float, fee_rate: float = DEFAULT_TAKER_FEE_RATE) -> float:
    """The taker fee per SHARE, in price units: fee_rate * p * (1 - p).
    Buying C shares at price p costs C * taker_fee_per_share(p) in fees.
    This is the unit that compares directly to edge_pct (|model - market|,
    also a per-share price difference) and to per-share combined costs like
    a sum-to-one pair's yes_ask + no_ask.

    Peaks at fee_rate * 0.25 (~1.5c per share at fee_rate=0.06, p=0.50);
    zero at p=0 and p=1.
    """
    p = min(max(price, 0.0), 1.0)
    return fee_rate * p * (1 - p)


def taker_fee_fraction_of_notional(price: float, fee_rate: float = DEFAULT_TAKER_FEE_RATE) -> float:
    """The taker fee as a fraction of the DOLLARS SPENT: fee_rate * (1 - p).
    Buying `size_usd` at price p costs `size_usd * taker_fee_fraction_of_notional(p)`
    in fees (since size = C * p and the per-share fee is rate*p*(1-p), the
    fee is (size/p) * rate*p*(1-p) = size * rate*(1-p)).

    ~3% of notional at p=0.50, ~1.3% at p=0.78, ~4.2% at p=0.30 — higher
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
