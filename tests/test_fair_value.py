"""
Tests for engine/fair_value.py — the reference-price-aware probability model
that replaces the old momentum-vs-current-price comparison.
"""
import math

import pytest

from engine.fair_value import FairValueInputs, RealizedVolatilityEstimator, fair_value_probability


# -- fair_value_probability -----------------------------------------------------

def test_at_reference_price_probability_is_half():
    inputs = FairValueInputs(
        current_price=65000, reference_price=65000, time_remaining_s=300, volatility_per_sqrt_s=5.0,
    )
    assert fair_value_probability(inputs) == pytest.approx(0.5, abs=1e-9)


def test_far_above_reference_probability_near_one():
    inputs = FairValueInputs(
        current_price=65500, reference_price=65000, time_remaining_s=60, volatility_per_sqrt_s=1.0,
    )
    assert fair_value_probability(inputs) > 0.99


def test_far_below_reference_probability_near_zero():
    inputs = FairValueInputs(
        current_price=64500, reference_price=65000, time_remaining_s=60, volatility_per_sqrt_s=1.0,
    )
    assert fair_value_probability(inputs) < 0.01


def test_no_time_remaining_is_hard_zero_or_one():
    above = FairValueInputs(current_price=65001, reference_price=65000, time_remaining_s=0, volatility_per_sqrt_s=5.0)
    below = FairValueInputs(current_price=64999, reference_price=65000, time_remaining_s=0, volatility_per_sqrt_s=5.0)
    assert fair_value_probability(above) == 1.0
    assert fair_value_probability(below) == 0.0


def test_more_time_remaining_pulls_probability_toward_half():
    """More time left = more uncertainty = probability should move toward 0.5,
    all else equal — this is the core behavior that makes the model
    'reference-aware' rather than just re-deriving the momentum heuristic."""
    near_expiry = FairValueInputs(current_price=65200, reference_price=65000, time_remaining_s=10, volatility_per_sqrt_s=5.0)
    far_from_expiry = FairValueInputs(current_price=65200, reference_price=65000, time_remaining_s=600, volatility_per_sqrt_s=5.0)
    p_near = fair_value_probability(near_expiry)
    p_far = fair_value_probability(far_from_expiry)
    assert p_near > p_far > 0.5  # both above 50% (price is above reference), but less confident with more time left


def test_higher_volatility_pulls_probability_toward_half():
    """The exact scenario the momentum-only heuristic got wrong: being above
    reference means little if the asset is volatile enough that a further
    swing either way is plausible before expiry."""
    low_vol = FairValueInputs(current_price=65200, reference_price=65000, time_remaining_s=300, volatility_per_sqrt_s=1.0)
    high_vol = FairValueInputs(current_price=65200, reference_price=65000, time_remaining_s=300, volatility_per_sqrt_s=20.0)
    assert fair_value_probability(low_vol) > fair_value_probability(high_vol) > 0.5


def test_the_reviewers_exact_example():
    """
    From the code review: BTC starts contract at $70,000, falls to $69,500,
    then rises to $69,650. Momentum over the recent window is UP, but price
    is still BELOW the $70,000 reference — the contract should still lean NO
    (below reference), not UP, despite positive recent momentum.
    """
    inputs = FairValueInputs(
        current_price=69650, reference_price=70000, time_remaining_s=300, volatility_per_sqrt_s=15.0,
    )
    p_finish_above = fair_value_probability(inputs)
    assert p_finish_above < 0.5  # correctly leans NO/below-reference despite recent upward momentum


# -- RealizedVolatilityEstimator -------------------------------------------

def test_volatility_estimator_returns_none_with_too_few_ticks():
    est = RealizedVolatilityEstimator(min_ticks=5)
    assert est.estimate([100, 101], [0, 1]) is None


def test_volatility_estimator_zero_for_perfectly_flat_prices():
    est = RealizedVolatilityEstimator(min_ticks=3)
    prices = [100.0] * 10
    timestamps = [float(i) for i in range(10)]
    result = est.estimate(prices, timestamps)
    assert result == pytest.approx(0.0, abs=1e-9)


def test_volatility_estimator_positive_for_noisy_prices():
    est = RealizedVolatilityEstimator(min_ticks=3)
    prices = [100.0, 101.0, 99.5, 100.8, 99.2, 101.5, 98.9, 100.1]
    timestamps = [float(i) for i in range(len(prices))]
    result = est.estimate(prices, timestamps)
    assert result is not None
    assert result > 0


def test_volatility_estimator_mismatched_lengths_returns_none():
    est = RealizedVolatilityEstimator()
    assert est.estimate([100, 101, 102], [0, 1]) is None


def test_volatility_estimator_ewma_weights_recent_ticks_more():
    """The EWMA machinery (explicit decay < 1.0) must make the estimate react
    faster to a volatility regime change: an old calm stretch followed by
    violent recent returns must read HIGHER than the flat average (decay=1.0).
    The DEFAULT is flat (decay=1.0) — EWMA is opt-in pending live data (see
    the class docstring for why), but the mechanism stays verified."""
    # 8 calm ticks (1s apart), then 4 violent ones — all within one 120s window.
    calm = [100.0] * 8
    violent = [100.0, 100.5, 101.0, 101.5, 102.0]  # large consecutive moves
    prices = calm + violent
    timestamps = [float(i) for i in range(len(prices))]

    flat = RealizedVolatilityEstimator(min_ticks=3, decay=1.0)
    ewma = RealizedVolatilityEstimator(min_ticks=3, decay=0.5)

    flat_sigma = flat.estimate(prices, timestamps)
    ewma_sigma = ewma.estimate(prices, timestamps)
    assert flat_sigma is not None and ewma_sigma is not None
    assert ewma_sigma > flat_sigma  # recent violence dominates with decay


def test_volatility_estimator_default_is_flat_average():
    """The shipped default (decay=1.0) must reproduce the exact flat-average
    behavior the whole strategy was validated on — a single jump after a
    calm stretch stays tradeable (not explained away as volatility)."""
    prices = [65000.0] * 200 + [65005.0, 66000.0]
    timestamps = [float(i) * 0.1 for i in range(len(prices))]
    timestamps[-2] = timestamps[-201] + 20.2
    timestamps[-1] = timestamps[-201] + 21.5

    est = RealizedVolatilityEstimator(min_ticks=3)  # defaults: decay=1.0, cap disabled
    sigma = est.estimate(prices, timestamps)
    assert sigma is not None
    assert sigma == pytest.approx(215.7, rel=0.01)  # the flat value, not ~300-970


def test_volatility_estimator_ewma_rejects_bad_decay():
    with pytest.raises(ValueError):
        RealizedVolatilityEstimator(decay=0.0)
    with pytest.raises(ValueError):
        RealizedVolatilityEstimator(decay=1.5)
    with pytest.raises(ValueError):
        RealizedVolatilityEstimator(max_contribution_fraction=0.0)
    with pytest.raises(ValueError):
        RealizedVolatilityEstimator(max_contribution_fraction=1.5)
