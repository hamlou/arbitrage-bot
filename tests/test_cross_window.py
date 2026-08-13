"""
Tests for engine/cross_window.py — the second risk-free leg: same-endTime
5m/15m windows resolve against the SAME final price but different beats, so
buying UP on the lower-beat window + DOWN on the higher-beat window pays
>= $1 for every outcome (the middle band pays $2).
"""
import pytest

from data.polymarket_feed import Market, OrderBook, OrderBookLevel
from engine.cross_window import (
    cross_window_payout,
    find_cross_window_opportunity,
    find_cross_window_pair,
)

T_END = 1_800_000_000.0  # shared endTime


def make_window(
    market_id: str,
    duration: int,
    reference: float,
    captured_delay_s: float = 2.0,
) -> Market:
    """A 5m/15m window ending at T_END whose reference was captured
    `captured_delay_s` seconds after its open."""
    open_ts = T_END - duration * 60
    return Market(
        market_id=market_id,
        question=f"Bitcoin Up or Down - {duration} min",
        token_id_yes=f"{market_id}_yes", token_id_no=f"{market_id}_no",
        liquidity_usd=100_000, end_date_iso="2026-08-13T14:00:00Z",
        asset="BTC", duration_minutes=duration,
        reference_price=reference,
        reference_captured_at=open_ts + captured_delay_s,
        expires_at_ts=T_END, category="crypto",
    )


def make_book(token_id: str, best_ask: float) -> OrderBook:
    return OrderBook(
        market_id="x", token_id=token_id,
        bids=(OrderBookLevel(price=best_ask - 0.02, size=50_000),),
        asks=(OrderBookLevel(price=best_ask, size=50_000),),
    )


# --- payoff math -------------------------------------------------------------

def test_payout_never_below_one_for_any_final_price():
    """The arb is valid iff payout >= 1 for every possible final price when
    beats are ordered lower < higher."""
    lower_beat, higher_beat = 64_000.0, 65_000.0
    for final in (63_000, 64_000, 64_500, 65_000, 65_001, 66_000):
        assert cross_window_payout(final, lower_beat, higher_beat) >= 1.0


def test_middle_band_pays_double():
    assert cross_window_payout(64_500, 64_000, 65_000) == pytest.approx(2.0)
    # Outer bands pay single
    assert cross_window_payout(63_000, 64_000, 65_000) == pytest.approx(1.0)
    assert cross_window_payout(66_000, 64_000, 65_000) == pytest.approx(1.0)


# --- pairing -----------------------------------------------------------------

def test_pair_finds_lower_and_higher_beat_windows():
    m5 = make_window("m5", 5, reference=64_000)     # lower beat
    m15 = make_window("m15", 15, reference=65_000)  # higher beat
    pair = find_cross_window_pair(
        [m5, m15], max_ref_capture_delay_s=10.0, min_beat_gap_pct=0.0001,
    )
    assert pair == (m5, m15)


def test_pair_orders_by_beat_not_by_duration():
    # 5m window has the HIGHER beat this cycle (BTC rose between T-15 and
    # T-5): the 15m window is now the lower-beat leg.
    m5 = make_window("m5", 5, reference=65_000)
    m15 = make_window("m15", 15, reference=64_000)
    pair = find_cross_window_pair(
        [m5, m15], max_ref_capture_delay_s=10.0, min_beat_gap_pct=0.0001,
    )
    assert pair == (m15, m5)


def test_pair_rejects_reference_captured_late():
    # Reference captured 5 minutes into the 15m window — far from the real
    # beat, so ordering cannot be trusted. Rejected.
    m5 = make_window("m5", 5, reference=64_000)
    m15 = make_window("m15", 15, reference=65_000, captured_delay_s=300.0)
    assert find_cross_window_pair(
        [m5, m15], max_ref_capture_delay_s=10.0, min_beat_gap_pct=0.0001,
    ) is None


def test_pair_rejects_unknown_capture_time():
    import dataclasses
    m5 = make_window("m5", 5, reference=64_000)
    m15 = make_window("m15", 15, reference=65_000, captured_delay_s=300.0)
    m15_unknown = dataclasses.replace(m15, reference_captured_at=None)
    assert find_cross_window_pair(
        [m5, m15_unknown], max_ref_capture_delay_s=10.0, min_beat_gap_pct=0.0001,
    ) is None


def test_pair_rejects_when_beats_too_close():
    # Beat gap below the minimum: an ordering mistake would flip the arb
    # into a directional bet, so no pair.
    m5 = make_window("m5", 5, reference=64_000)
    m15 = make_window("m15", 15, reference=64_001)  # 0.0015% gap
    assert find_cross_window_pair(
        [m5, m15], max_ref_capture_delay_s=10.0, min_beat_gap_pct=0.0005,
    ) is None


def test_pair_requires_both_durations():
    m5 = make_window("m5", 5, reference=64_000)
    assert find_cross_window_pair(
        [m5], max_ref_capture_delay_s=10.0, min_beat_gap_pct=0.0001,
    ) is None


# --- opportunity detection ---------------------------------------------------

def test_detects_genuine_cross_window_arb():
    m5 = make_window("m5", 5, reference=64_000)   # lower beat -> buy UP
    m15 = make_window("m15", 15, reference=65_000)  # higher beat -> buy DOWN
    opp = find_cross_window_opportunity(
        m5, m15,
        make_book(m5.token_id_yes, 0.46),   # 5m UP ask
        make_book(m15.token_id_no, 0.48),   # 15m DOWN ask
        min_edge_pct=0.01, fee_pct=0.02,
    )
    assert opp is not None
    assert opp.side_a == "YES" and opp.side_b == "NO"
    assert opp.token_id_a == m5.token_id_yes
    assert opp.token_id_b == m15.token_id_no
    assert opp.combined_cost == pytest.approx(0.94)
    assert opp.net_profit_pct > 0.01


def test_no_opportunity_when_correctly_priced():
    m5 = make_window("m5", 5, reference=64_000)
    m15 = make_window("m15", 15, reference=65_000)
    # 0.52 + 0.50 = 1.02 — no arbitrage.
    opp = find_cross_window_opportunity(
        m5, m15,
        make_book(m5.token_id_yes, 0.52),
        make_book(m15.token_id_no, 0.50),
        min_edge_pct=0.01, fee_pct=0.02,
    )
    assert opp is None


def test_fees_can_erase_a_marginal_opportunity():
    m5 = make_window("m5", 5, reference=64_000)
    m15 = make_window("m15", 15, reference=65_000)
    # 0.49 + 0.50 = 0.99 -> 1% raw edge; the crypto fee rate eats it.
    opp = find_cross_window_opportunity(
        m5, m15,
        make_book(m5.token_id_yes, 0.49),
        make_book(m15.token_id_no, 0.50),
        min_edge_pct=0.01, fee_pct=0.07,
    )
    assert opp is None


def test_missing_ask_returns_none():
    m5 = make_window("m5", 5, reference=64_000)
    m15 = make_window("m15", 15, reference=65_000)
    empty = OrderBook(market_id="x", token_id="t", bids=(), asks=())
    opp = find_cross_window_opportunity(
        m5, m15, empty, make_book(m15.token_id_no, 0.48),
        min_edge_pct=0.01, fee_pct=0.02,
    )
    assert opp is None
