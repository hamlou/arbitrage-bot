"""
Tests for engine/sum_to_one.py's opportunity-detection logic.
"""
import pytest

from data.polymarket_feed import Market, OrderBook, OrderBookLevel
from engine.sum_to_one import find_sum_to_one_opportunity


def make_market() -> Market:
    return Market(
        market_id="m1", question="Bitcoin Up or Down - 15 min",
        token_id_yes="tok_yes", token_id_no="tok_no",
        liquidity_usd=100_000, end_date_iso="2026-07-31T14:00:00Z",
        asset="BTC", duration_minutes=15,
    )


def make_book(best_ask: float) -> OrderBook:
    return OrderBook(
        market_id="m1", token_id="tok",
        bids=(OrderBookLevel(price=best_ask - 0.02, size=1000),),
        asks=(OrderBookLevel(price=best_ask, size=1000),),
    )


def test_detects_genuine_mispricing():
    # YES ask 0.46 + NO ask 0.48 = 0.94 -> 6% profit before fees
    yes_book = make_book(0.46)
    no_book = make_book(0.48)
    opp = find_sum_to_one_opportunity(make_market(), yes_book, no_book, min_edge_pct=0.01, fee_pct=0.02)

    assert opp is not None
    assert opp.combined_cost == pytest.approx(0.94)
    assert opp.net_profit_pct > 0.01


def test_no_opportunity_when_correctly_priced():
    # YES ask 0.52 + NO ask 0.50 = 1.02 -> no arbitrage, sums above $1
    yes_book = make_book(0.52)
    no_book = make_book(0.50)
    opp = find_sum_to_one_opportunity(make_market(), yes_book, no_book, min_edge_pct=0.01, fee_pct=0.02)
    assert opp is None


def test_fees_can_erase_a_marginal_opportunity():
    # 0.49 + 0.50 = 0.99 -> 1% raw edge, but a high fee_pct should wipe it out
    yes_book = make_book(0.49)
    no_book = make_book(0.50)
    opp = find_sum_to_one_opportunity(make_market(), yes_book, no_book, min_edge_pct=0.01, fee_pct=0.05)
    assert opp is None


def test_missing_ask_returns_none():
    yes_book = OrderBook(market_id="m1", token_id="tok_yes", bids=(), asks=())
    no_book = make_book(0.48)
    opp = find_sum_to_one_opportunity(make_market(), yes_book, no_book, min_edge_pct=0.01, fee_pct=0.02)
    assert opp is None


def test_edge_threshold_respected():
    # 0.47 + 0.48 = 0.95 -> 5% raw edge; with a 2% fee, net ~3% -- should pass
    # a 1% threshold but fail a 5% threshold.
    yes_book = make_book(0.47)
    no_book = make_book(0.48)
    assert find_sum_to_one_opportunity(make_market(), yes_book, no_book, min_edge_pct=0.01, fee_pct=0.02) is not None
    assert find_sum_to_one_opportunity(make_market(), yes_book, no_book, min_edge_pct=0.05, fee_pct=0.02) is None


def test_category_aware_fee_rate_overrides_fee_pct():
    """The market's category drives the fee rate (added 2026-08-12): a
    geopolitics pair (fee-free) must be detected even when the same pair on
    a crypto market (or with a high fallback rate) is erased by fees."""
    yes_book = make_book(0.49)
    no_book = make_book(0.50)

    # Crypto market: 0.49+0.50=0.99 -> 1% raw edge is eaten by the 0.07 rate.
    crypto = Market(
        market_id="m1", question="Bitcoin Up or Down - 15 min",
        token_id_yes="tok_yes", token_id_no="tok_no",
        liquidity_usd=100_000, end_date_iso="2026-07-31T14:00:00Z",
        asset="BTC", duration_minutes=15, category="crypto",
    )
    assert find_sum_to_one_opportunity(crypto, yes_book, no_book, min_edge_pct=0.01, fee_pct=0.07) is None

    # Same pair on geopolitics: zero fee -> 1.01% net edge -> detected.
    geo = Market(
        market_id="m2", question="Will X happen?",
        token_id_yes="tok_yes", token_id_no="tok_no",
        liquidity_usd=100_000, end_date_iso="2026-07-31T14:00:00Z",
        category="geopolitics",
    )
    opp = find_sum_to_one_opportunity(geo, yes_book, no_book, min_edge_pct=0.01, fee_pct=0.07)
    assert opp is not None
    assert opp.net_profit_pct == pytest.approx(0.01 / 0.99)


def test_unknown_category_uses_fallback_fee_pct():
    # No category on the market -> the passed fee_pct is honored (the
    # configured crypto rate), not silently treated as "other".
    market = make_market()  # category defaults to None
    yes_book = make_book(0.49)
    no_book = make_book(0.50)
    assert find_sum_to_one_opportunity(market, yes_book, no_book, min_edge_pct=0.01, fee_pct=0.07) is None
