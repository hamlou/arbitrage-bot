"""
Sum-to-one (combo) arbitrage: in a correctly priced binary market, YES_ask +
NO_ask should sit at or above $1.00 (otherwise buying both sides for less
than $1 total would guarantee a $1 payout regardless of outcome — free
money). Fragmented order books or momentary mispricing can occasionally let
this gap open.

Unlike the latency/fair-value strategy, this doesn't depend on forecasting
direction at all — it's arithmetic, not prediction. That also means it gets
its own position cap (SUM_TO_ONE_MAX_POSITION_PCT) rather than sharing
Kelly-based sizing with the directional strategy, since Kelly sizing assumes
a probabilistic edge this strategy doesn't have.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from data.polymarket_feed import Market, OrderBook
from engine.fees import fee_rate_for_category, taker_fee_pct


@dataclass(frozen=True, slots=True)
class SumToOneOpportunity:
    market: Market
    yes_ask: float
    no_ask: float
    combined_cost: float           # yes_ask + no_ask, before fees
    net_profit_pct: float          # guaranteed profit per dollar staked, AFTER modeled fees


def find_sum_to_one_opportunity(
    market: Market,
    yes_book: OrderBook,
    no_book: OrderBook,
    min_edge_pct: float,
    fee_pct: float,
) -> Optional[SumToOneOpportunity]:
    yes_ask = yes_book.best_ask
    no_ask = no_book.best_ask
    if yes_ask is None or no_ask is None:
        return None

    combined_cost = yes_ask + no_ask
    if combined_cost <= 0:
        return None

    profit_before_fees = 1.0 - combined_cost
    # Price-dependent taker fees (fee_rate * p * (1 - p) per share), charged
    # on both legs. The RATE is CATEGORY-aware (docs.polymarket.com/trading/
    # fees, added 2026-08-12): geopolitics is fee-free, politics/finance
    # 0.04, crypto 0.07. A sub-$1 pair in a fee-free category is pure profit;
    # charging it the crypto rate would hide exactly the opportunities the
    # risk-free scan exists to find. fee_pct (the configured crypto rate) is
    # the fallback for markets whose category is unknown.
    rate = fee_rate_for_category(market.category) if market.category else fee_pct
    fee_cost = taker_fee_pct(yes_ask, rate) + taker_fee_pct(no_ask, rate)
    net_profit_pct = (profit_before_fees - fee_cost) / combined_cost

    if net_profit_pct <= min_edge_pct:
        return None

    return SumToOneOpportunity(
        market=market, yes_ask=yes_ask, no_ask=no_ask,
        combined_cost=combined_cost, net_profit_pct=net_profit_pct,
    )
