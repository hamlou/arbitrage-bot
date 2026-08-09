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

    EWMA weighting (decay < 1.0): squared log returns are weighted so the
    MOST RECENT ticks dominate the estimate — the standard fix for a flat
    lookback that understates near-term risk during a volatility regime
    change (news, liquidation cascade).

    WHY EWMA IS OFF BY DEFAULT (verified 2026-08-09, not shipped): the
    strategy's edge IS a fresh move, and a geometric EWMA over-weights it.
    Measured on the repo's own scenarios, NO decay + cap combination
    satisfies both required behaviors:
      - a single jump after a calm stretch (the fast-path test's 1.5% move)
        inflated sigma ~4x, collapsing the fair-value edge below threshold;
      - a SUSTAINED move (9 consecutive ~0.3% returns) had its vol estimate
        crushed by the contribution cap, inflating the z-score until the
        model hit the saturation guard instead of the fresh-move gate.
    The flat average handles both correctly: one jump is treated as a level
    change (tradeable), sustained moves as volatility (not tradeable).
    Keep decay=1.0 (flat) unless a live run with order-book data shows the
    regime lag is actually losing money; the machinery below (decay +
    max_contribution_fraction) is tested and ready to tune when that data
    exists.

    SINGLE-TICK CAP: a plain geometric EWMA gives the LAST return up to
    (1-decay) of the total weight when a jump follows a long calm stretch.
    max_contribution_fraction caps any ONE return's share of the VARIANCE
    ESTIMATE ITSELF (0.10 = a lone spike can't count for more than a tenth
    of the estimate), with the clipped weight spread back over the other
    returns. Set max_contribution_fraction=1.0 to disable the cap.
    """

    def __init__(
        self,
        min_ticks: int = 5,
        decay: float = 1.0,
        max_contribution_fraction: float = 1.0,  # disabled by default: decay=1.0 must stay exactly flat
    ):
        self.min_ticks = min_ticks
        if not (0.0 < decay <= 1.0):
            raise ValueError("decay must be in (0, 1]")
        if not (0.0 < max_contribution_fraction <= 1.0):
            raise ValueError("max_contribution_fraction must be in (0, 1]")
        self.decay = decay
        self.max_contribution_fraction = max_contribution_fraction

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
        if mean_dt <= 0:
            return None

        # EWMA over returns: weight r_i by decay^(n-1-i) — the LAST (most
        # recent) return always gets weight 1, older ones decay geometrically.
        # decay=1.0 collapses to the plain average (previous behavior).
        n = len(log_returns)
        weights = [self.decay ** (n - 1 - i) for i in range(n)]
        total_w = sum(weights)

        # Cap any single return's VARIANCE CONTRIBUTION (w * r^2) so one jump
        # can't dominate (see class docstring). The excess weight is spread
        # back over the remaining returns, preserving the EWMA's regime
        # response in normal conditions. The cap is applied to contributions
        # computed with the final (capped) weights, so iterate twice:
        # (1) provisional contributions -> cap on the largest, (2) distribute
        # the freed weight and re-check (bounded, converges in a few passes
        # since capping only ever reduces the largest contributions).
        cap = self.max_contribution_fraction * max(
            w * r * r for w, r in zip(weights, log_returns)
        )
        if cap < max(w * r * r for w, r in zip(weights, log_returns)):
            for _ in range(8):
                contributions = [w * r * r for w, r in zip(weights, log_returns)]
                idx = contributions.index(max(contributions))
                if contributions[idx] <= cap:
                    break
                excess = contributions[idx] - cap
                weights[idx] = cap / (log_returns[idx] ** 2)
                # Spread the freed weight over the OTHER returns proportionally.
                others = [j for j in range(n) if j != idx]
                other_total = sum(weights[j] for j in others)
                if other_total <= 0:
                    break
                for j in others:
                    weights[j] += excess * (weights[j] / other_total)

        variance = sum(w * r * r for w, r in zip(weights, log_returns)) / total_w

        sigma_log_per_sqrt_s = math.sqrt(variance / mean_dt)
        current_price = prices[-1]
        return sigma_log_per_sqrt_s * current_price  # linear approx: price-unit sigma
