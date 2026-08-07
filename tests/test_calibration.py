"""
Tests for engine/calibration.py: the binned fitting logic and the
sliding-window sample builder, using synthetic data with a known relationship
so we can check the fit actually recovers something sensible.
"""
import random

import pytest

from engine.calibration import (
    CalibrationModel,
    build_samples_from_price_series,
    fit_calibration,
)


def _synthetic_samples(n: int, seed: int = 0) -> list[tuple[float, bool]]:
    """
    Generates samples where larger momentum magnitude genuinely does predict
    a higher continuation probability, so a correct fit should recover an
    increasing curve.
    """
    rng = random.Random(seed)
    samples = []
    for _ in range(n):
        magnitude = rng.uniform(0.0, 0.02)
        true_p = min(0.95, 0.5 + magnitude * 15)  # designed-in relationship
        held = rng.random() < true_p
        samples.append((magnitude, held))
    return samples


# -- fit_calibration -----------------------------------------------------------

def test_fit_returns_none_with_too_little_data():
    samples = _synthetic_samples(10)
    assert fit_calibration(samples, horizon_minutes=15, n_bins=8) is None


def test_fit_recovers_increasing_relationship():
    samples = _synthetic_samples(2000, seed=42)
    model = fit_calibration(samples, horizon_minutes=15, n_bins=8)

    assert model is not None
    assert model.sample_count == 2000
    # Larger magnitude should map to a higher (or at least equal) continuation probability.
    assert model.continuation_probability == sorted(model.continuation_probability)


def test_fit_never_predicts_below_50pct():
    samples = _synthetic_samples(2000, seed=1)
    model = fit_calibration(samples, horizon_minutes=15, n_bins=8)
    assert all(p >= 0.5 for p in model.continuation_probability)


def test_fitted_model_probability_direction_holds_interpolates():
    samples = _synthetic_samples(2000, seed=7)
    model = fit_calibration(samples, horizon_minutes=15, n_bins=8)

    p_small = model.probability_direction_holds(0.0005)
    p_large = model.probability_direction_holds(0.018)
    assert p_large >= p_small  # bigger observed moves -> at least as confident


def test_implied_probability_up_direction_above_half():
    samples = _synthetic_samples(2000, seed=3)
    model = fit_calibration(samples, horizon_minutes=15, n_bins=8)

    p = model.implied_probability(momentum_pct=0.01, direction="UP")
    assert p > 0.5


def test_implied_probability_down_direction_below_half():
    samples = _synthetic_samples(2000, seed=3)
    model = fit_calibration(samples, horizon_minutes=15, n_bins=8)

    p = model.implied_probability(momentum_pct=-0.01, direction="DOWN")
    assert p < 0.5


# -- isotonic monotonicity (pool-adjacent-violators) -----------------------------


def test_pav_pools_only_violations():
    from engine.calibration import _isotonic_pav
    # Only the descending pair (0.6, 0.5) is pooled -> both become their mean;
    # the already-monotonic stretches are untouched.
    assert _isotonic_pav([0.4, 0.6, 0.5, 0.7], [1, 1, 1, 1]) == pytest.approx([0.4, 0.55, 0.55, 0.7])


def test_pav_does_not_copy_peak_forward():
    from engine.calibration import _isotonic_pav
    # The 2026-08-07 artifact: np.maximum.accumulate locked one noisy bin as
    # the floor for every later bin (7 of 8 identical values). PAV must NOT do
    # that — the dip after the peak pools with its neighbors instead.
    rates = [0.5019660735591638, 0.5195829226539399, 0.50, 0.51, 0.52]
    out = _isotonic_pav(rates, [1] * 5)
    assert out[1] != out[4]  # the peak was NOT propagated to the end
    assert out == sorted(out)  # still monotone non-decreasing


def test_pav_is_count_weighted():
    from engine.calibration import _isotonic_pav
    # A violation with unequal bin counts must be pooled by weighted mean.
    assert _isotonic_pav([0.7, 0.5], [1, 3]) == pytest.approx([0.55, 0.55])


# -- CalibrationModel serialization ---------------------------------------------

def test_model_round_trips_through_dict():
    samples = _synthetic_samples(1000, seed=5)
    model = fit_calibration(samples, horizon_minutes=5, n_bins=6)
    restored = CalibrationModel.from_dict(model.to_dict())
    assert restored.horizon_minutes == model.horizon_minutes
    assert restored.magnitude_breakpoints == model.magnitude_breakpoints
    assert restored.continuation_probability == model.continuation_probability


# -- build_samples_from_price_series -------------------------------------------

def test_build_samples_detects_clean_uptrend_continuation():
    # A steadily rising price series: momentum computed at any point should
    # predict (correctly) that the price is still higher `horizon_s` later.
    timestamps = [float(i) for i in range(200)]
    prices = [100.0 + 0.5 * i for i in range(200)]  # steady uptrend, no noise

    samples = build_samples_from_price_series(timestamps, prices, lookback_s=10, horizon_s=10)

    assert len(samples) > 0
    # In a clean, monotonic uptrend, momentum should "hold" essentially always.
    held_fraction = sum(1 for _, held in samples if held) / len(samples)
    assert held_fraction > 0.95


def test_build_samples_detects_clean_reversal():
    # Price rises then sharply reverses — momentum measured near the peak
    # should predict UP but the outcome should be DOWN (held=False).
    timestamps = [float(i) for i in range(100)]
    prices = [100.0 + i for i in range(50)] + [149.0 - i for i in range(50)]

    samples = build_samples_from_price_series(timestamps, prices, lookback_s=5, horizon_s=20)
    assert len(samples) > 0
    # Not all samples should "hold" in this series — some sit right at the reversal.
    held_flags = [held for _, held in samples]
    assert False in held_flags


def test_build_samples_empty_series_returns_empty():
    assert build_samples_from_price_series([], [], lookback_s=10, horizon_s=10) == []
