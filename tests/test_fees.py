"""
Tests for engine/fees.py -- Polymarket's price-dependent crypto taker fee.

The real fee (docs.polymarket.com/trading/fees) is fee_rate * p * (1-p) per
share, NOT a flat fraction of size. This replaced the flat-2% assumption that
understated mid-price fees (paper results looked better than live would be).
"""
import pytest

from engine.fees import (
    DEFAULT_TAKER_FEE_RATE,
    round_trip_fee_pct,
    taker_fee_fraction_of_notional,
    taker_fee_per_share,
)


def test_fee_is_zero_at_extremes():
    assert taker_fee_per_share(0.0) == 0.0
    assert taker_fee_per_share(1.0) == 0.0
    assert taker_fee_fraction_of_notional(1.0) == 0.0


def test_fee_peaks_at_midpoint():
    # Per share: 0.07 * 0.5 * 0.5 = 1.75c on a 50c share at p=0.50.
    assert taker_fee_per_share(0.50) == pytest.approx(0.0175)
    assert taker_fee_per_share(0.50, fee_rate=DEFAULT_TAKER_FEE_RATE) == pytest.approx(0.0175)


def test_fee_is_price_dependent():
    # Lower at the edges than at the midpoint; higher at LOW prices than high.
    assert taker_fee_per_share(0.30) < taker_fee_per_share(0.50)
    assert taker_fee_per_share(0.78) < taker_fee_per_share(0.50)
    assert taker_fee_per_share(0.30) > taker_fee_per_share(0.78)


def test_fee_as_fraction_of_notional():
    # fee / notional = fee_rate * (1 - p): ~3.5% of spend at p=0.5.
    assert taker_fee_fraction_of_notional(0.50) == pytest.approx(0.035)
    assert taker_fee_fraction_of_notional(0.78) == pytest.approx(0.07 * 0.22)
    assert taker_fee_fraction_of_notional(0.30) == pytest.approx(0.07 * 0.70)


def test_dollar_fee_matches_docs_example():
    # Docs example: 100 shares at $0.50 -> fee = 100 * 0.07 * 0.5 * 0.5 =
    # $1.75 on $50 notional = 3.5%. The dollar fee must be computed as
    # notional * fraction_of_notional, NOT notional * per_share_fee (which
    # would wrongly halve it at p=0.5).
    shares = 100
    price = 0.50
    notional = shares * price
    assert notional * taker_fee_fraction_of_notional(price) == pytest.approx(1.75)
    assert shares * taker_fee_per_share(price) == pytest.approx(1.75)


def test_round_trip_fee_is_entry_plus_exit():
    # A taker round trip at p=0.5 pays the per-share fee twice: 1.75c each.
    assert round_trip_fee_pct(0.50) == pytest.approx(0.035)
    assert round_trip_fee_pct(0.50, exit_price=0.60) == pytest.approx(
        taker_fee_per_share(0.50) + taker_fee_per_share(0.60)
    )


def test_out_of_range_prices_are_clamped():
    assert taker_fee_per_share(-1.0) == 0.0
    assert taker_fee_per_share(2.0) == 0.0
