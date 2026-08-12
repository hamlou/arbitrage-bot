"""
Tests for engine/fees.py -- Polymarket's price-dependent crypto taker fee.

The real fee (docs.polymarket.com/trading/fees) is fee_rate * p * (1-p) per
share, NOT a flat fraction of size. This replaced the flat-2% assumption that
understated mid-price fees (paper results looked better than live would be).
"""
import pytest

from engine.fees import (
    CATEGORY_FEE_RATES,
    DEFAULT_TAKER_FEE_RATE,
    fee_rate_for_category,
    round_trip_fee_pct,
    taker_fee_fraction_of_notional,
    taker_fee_per_share,
)


def test_fee_is_zero_at_extremes():
    assert taker_fee_per_share(0.0) == 0.0
    assert taker_fee_per_share(1.0) == 0.0
    assert taker_fee_fraction_of_notional(1.0) == 0.0


def test_fee_peaks_at_midpoint():
    # Per share: 0.07 * 0.5 * 0.5 = 1.75c on a 50c share at p=0.50
    # (crypto rate, confirmed 2026-08-12 against docs.polymarket.com).
    assert taker_fee_per_share(0.50) == pytest.approx(0.0175)
    assert taker_fee_per_share(0.50, fee_rate=DEFAULT_TAKER_FEE_RATE) == pytest.approx(0.0175)


def test_fee_is_price_dependent():
    # Lower at the edges than at the midpoint; higher at LOW prices than high.
    assert taker_fee_per_share(0.30) < taker_fee_per_share(0.50)
    assert taker_fee_per_share(0.78) < taker_fee_per_share(0.50)
    assert taker_fee_per_share(0.30) > taker_fee_per_share(0.78)


def test_fee_as_fraction_of_notional():
    # fee / notional = fee_rate * (1 - p): ~3.5% of spend at p=0.5 (crypto
    # rate 0.07, confirmed against the live docs 2026-08-12).
    assert taker_fee_fraction_of_notional(0.50) == pytest.approx(0.035)
    assert taker_fee_fraction_of_notional(0.78) == pytest.approx(0.07 * 0.22)
    assert taker_fee_fraction_of_notional(0.30) == pytest.approx(0.07 * 0.70)


def test_dollar_fee_matches_docs_example():
    # Docs formula (docs.polymarket.com/trading/fees): fee = rate * C * p *
    # (1 - p). At the crypto rate 0.07, 1,000 contracts at $0.50 cost
    # 0.07 * 1,000 * 0.50 * 0.50 = $17.50 on $500 notional = 3.5%. The
    # dollar fee must be computed as notional * fraction_of_notional, NOT
    # notional * per_share_fee (which would wrongly halve it at p=0.5).
    shares = 1000
    price = 0.50
    notional = shares * price
    assert notional * taker_fee_fraction_of_notional(price) == pytest.approx(17.50)
    assert shares * taker_fee_per_share(price) == pytest.approx(17.50)


def test_round_trip_fee_is_entry_plus_exit():
    # A taker round trip at p=0.5 pays the per-share fee twice: 1.75c each
    # at the crypto rate.
    assert round_trip_fee_pct(0.50) == pytest.approx(0.035)
    assert round_trip_fee_pct(0.50, exit_price=0.60) == pytest.approx(
        taker_fee_per_share(0.50) + taker_fee_per_share(0.60)
    )


def test_out_of_range_prices_are_clamped():
    assert taker_fee_per_share(-1.0) == 0.0
    assert taker_fee_per_share(2.0) == 0.0


# -- category-aware rates (added 2026-08-12, docs.polymarket.com/trading/fees) --


def test_category_rates_match_official_schedule():
    assert CATEGORY_FEE_RATES["crypto"] == 0.07
    assert CATEGORY_FEE_RATES["geopolitics"] == 0.0   # fee-free
    assert CATEGORY_FEE_RATES["politics"] == 0.04
    assert CATEGORY_FEE_RATES["finance"] == 0.04
    assert CATEGORY_FEE_RATES["sports"] == 0.05


def test_fee_rate_for_category_is_case_insensitive_and_tag_friendly():
    assert fee_rate_for_category("Geopolitics") == 0.0
    assert fee_rate_for_category("Politics") == 0.04
    assert fee_rate_for_category("Crypto") == 0.07
    assert fee_rate_for_category("crypto") == 0.07
    assert fee_rate_for_category("Financial") == 0.04  # tag label, not exact key


def test_fee_rate_for_category_unknown_falls_back_to_other_not_crypto():
    # Unknown/missing categories must NOT be charged the crypto rate — the
    # point of category-awareness is that only crypto pays 0.07.
    assert fee_rate_for_category(None) == 0.05
    assert fee_rate_for_category("") == 0.05
    assert fee_rate_for_category("banana") == 0.05


def test_geopolitics_pair_is_fee_free_in_dollars():
    # A sub-$1 pair on a geopolitics market: zero fee on both legs.
    assert taker_fee_fraction_of_notional(0.60, fee_rate_for_category("geopolitics")) == 0.0
    assert taker_fee_fraction_of_notional(0.35, fee_rate_for_category("geopolitics")) == 0.0
