"""
Reference-price-aware fair value model.

This directly addresses the biggest structural flaw in the original signal
design: the momentum heuristic compared recent price momentum to the CURRENT
Polymarket price, but never asked what the contract actually resolves on —
whether the asset finishes above or below its REFERENCE price (the price at
contract open), not whether it's been moving recently in some direction.

A market can have strong upward momentum over the last 30 seconds while still
sitting well below its reference price — the "UP" signal from pure momentum
would be directionally wrong for what the contract actually pays out on.

Model: treat the short remaining window as a driftless (zero-drift) random
walk — a standard, defensible simplification for minutes-scale crypto price
moves, where any real drift is negligible relative to volatility over such a
short horizon. Under that assumption, the probability that price finishes
above the reference price is the same probability used to price a digital
(binary) option:

    P(finish above reference) = Φ( (current_price - reference_price) /
                                    (sigma_per_sqrt_s * sqrt(time_remaining_s)) )

where Φ is the standard normal CDF and sigma_per_sqrt_s is the asset's
realized volatility, estimated from recent tick data (see
RealizedVolatilityEstimator below).

This doesn't claim to know anything Polymarket doesn't — it's the same
"fair value" calculation an efficient market maker on Polymarket's side
should itself be running. The tradeable edge appears when there's a gap
between this and Polymarket's ACTUAL current price, which happens exactly
when Polymarket hasn't yet caught up to a recent price move — the real
mechanism the whole strategy is trying to exploit, now actually being
measured instead of assumed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass(frozen=True, slots=True)
class FairValueInputs:
    current_price: float
    reference_price: float
    time_remaining_s: float
    volatility_per_sqrt_s: float  # in PRICE units per sqrt(second), see below


def fair_value_probability(inputs: FairValueInputs) -> float:
    """
    P(asset finishes above reference_price), under a zero-drift Brownian
    motion assumption. Degenerate cases (no time left, or no usable
    volatility estimate) fall back to a hard 0/1 based on current position
    relative to the reference — there's no meaningful "probability" left to
    compute once the clock has run out.
    """
    if inputs.time_remaining_s <= 0:
        return 1.0 if inputs.current_price > inputs.reference_price else 0.0
    if inputs.volatility_per_sqrt_s <= 0:
        return 1.0 if inputs.current_price > inputs.reference_price else 0.0

    z = (inputs.current_price - inputs.reference_price) / (
        inputs.volatility_per_sqrt_s * math.sqrt(inputs.time_remaining_s)
    )
    return _normal_cdf(z)


class RealizedVolatilityEstimator:
    """
    Estimates an asset's short-horizon realized volatility from recent tick
    data, in price units per sqrt(second) — the units fair_value_probability
    needs. Uses zero-mean log-return variance (standard for short horizons,
    where estimating a reliable drift from noisy tick data isn't meaningful
    anyway) divided by average tick spacing, then converts back to price
    units via a linear approximation (valid for the small returns typical
    between consecutive ticks).
    """

    def __init__(self, min_ticks: int = 5):
        self.min_ticks = min_ticks

    def estimate(self, prices: list[float], timestamps: list[float]) -> Optional[float]:
        if len(prices) < self.min_ticks or len(prices) != len(timestamps):
            return None

        log_returns: list[float] = []
        dts: list[float] = []
        for (p_a, t_a), (p_b, t_b) in zip(zip(prices, timestamps), zip(prices[1:], timestamps[1:])):
            if p_a <= 0 or p_b <= 0:
                continue
            dt = t_b - t_a
            if dt <= 0:
                continue
            log_returns.append(math.log(p_b / p_a))
            dts.append(dt)

        if not log_returns:
            return None

        mean_dt = sum(dts) / len(dts)
        variance = sum(r * r for r in log_returns) / len(log_returns)
        if mean_dt <= 0:
            return None

        sigma_log_per_sqrt_s = math.sqrt(variance / mean_dt)
        current_price = prices[-1]
        return sigma_log_per_sqrt_s * current_price  # linear approx: price-unit sigma
