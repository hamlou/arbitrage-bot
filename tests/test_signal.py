"""
Tests for engine/signal.py. Uses synthetic PriceUpdate ticks and a fixture-
based order book; never touches a live endpoint.
"""
import time

import pytest

from data.binance_feed import PriceUpdate
from data.polymarket_feed import Market, OrderBook, OrderBookLevel
from engine.signal import SymbolMomentumTracker, _confidence_score, _implied_prob_from_momentum


def make_market(market_id="m1", asset="BTC") -> Market:
    return Market(
        market_id=market_id,
        question="Bitcoin Up or Down - 15 min",
        token_id_yes="tok_yes",
        token_id_no="tok_no",
        liquidity_usd=100_000,
        end_date_iso="2026-07-31T14:00:00Z",
        asset=asset,
        duration_minutes=15,
    )


def make_book(mid: float) -> OrderBook:
    spread = 0.01
    return OrderBook(
        market_id="m1",
        token_id="tok_yes",
        bids=(OrderBookLevel(price=mid - spread, size=5000),),
        asks=(OrderBookLevel(price=mid + spread, size=5000),),
    )


# -- SymbolMomentumTracker --------------------------------------------------

def test_momentum_tracker_requires_min_ticks_for_confirmation():
    tracker = SymbolMomentumTracker()
    tracker.add(PriceUpdate(symbol="BTCUSDT", price=100, event_time_ms=0, received_at=time.time(), kind="trade"))
    assert tracker.direction_confirmed() is None  # only 1 tick


def test_momentum_tracker_confirms_up_direction():
    tracker = SymbolMomentumTracker()
    now = time.time()
    for i, price in enumerate([100, 101, 102]):
        tracker.add(PriceUpdate(symbol="BTCUSDT", price=price, event_time_ms=0, received_at=now + i, kind="trade"))
    assert tracker.direction_confirmed() == "UP"


def test_momentum_tracker_confirms_down_direction():
    tracker = SymbolMomentumTracker()
    now = time.time()
    for i, price in enumerate([100, 99, 98]):
        tracker.add(PriceUpdate(symbol="BTCUSDT", price=price, event_time_ms=0, received_at=now + i, kind="trade"))
    assert tracker.direction_confirmed() == "DOWN"


def test_momentum_tracker_rejects_noisy_non_monotonic_ticks():
    tracker = SymbolMomentumTracker()
    now = time.time()
    for i, price in enumerate([100, 101, 100.5]):  # up then down -> not confirmed
        tracker.add(PriceUpdate(symbol="BTCUSDT", price=price, event_time_ms=0, received_at=now + i, kind="trade"))
    assert tracker.direction_confirmed() is None


def test_momentum_pct_computes_change_over_window():
    tracker = SymbolMomentumTracker(lookback_s=60)
    now = time.time()
    tracker.add(PriceUpdate(symbol="BTCUSDT", price=100, event_time_ms=0, received_at=now, kind="trade"))
    tracker.add(PriceUpdate(symbol="BTCUSDT", price=106, event_time_ms=0, received_at=now + 10, kind="trade"))
    assert tracker.momentum_pct() == pytest.approx(0.06)


# -- Implied probability mapping ------------------------------------------------

def test_implied_prob_up_moves_probability_above_half():
    p = _implied_prob_from_momentum(0.01, "UP")
    assert p > 0.5


def test_implied_prob_down_moves_probability_below_half():
    p = _implied_prob_from_momentum(0.01, "DOWN")
    assert p < 0.5


def test_implied_prob_saturates_within_bounds():
    p_up = _implied_prob_from_momentum(0.5, "UP")   # huge move
    p_down = _implied_prob_from_momentum(0.5, "DOWN")
    assert 0.0 < p_up <= 0.99
    assert 0.01 <= p_down < 1.0


# -- Confidence score ------------------------------------------------------------

def test_confidence_score_high_when_fresh_deep_and_confirmed():
    score = _confidence_score(tick_age_s=0.2, book_depth_usd=200_000, direction_confirmed=True, min_liquidity_usd=50_000)
    assert score > 0.8


def test_confidence_score_low_when_stale_shallow_unconfirmed():
    score = _confidence_score(tick_age_s=10.0, book_depth_usd=100, direction_confirmed=False, min_liquidity_usd=50_000)
    assert score < 0.2


def test_confidence_score_none_tick_age_scores_zero_freshness():
    score_none = _confidence_score(tick_age_s=None, book_depth_usd=200_000, direction_confirmed=True, min_liquidity_usd=50_000)
    score_fresh = _confidence_score(tick_age_s=0.1, book_depth_usd=200_000, direction_confirmed=True, min_liquidity_usd=50_000)
    assert score_none < score_fresh
