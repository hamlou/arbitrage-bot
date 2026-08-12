"""
Tests for the sizing/exit helpers added 2026-08-12 (Phase 2 of the external
review): _sizing_inputs (Kelly must see the ACTUAL side's executable price
and the POST-FEE edge, not always the YES mid and the raw gap) and
_median_reprice_hold_s (the measured reprice time behind the gap-timed exit).
"""
from __future__ import annotations

import pytest

from data.polymarket_feed import Market, OrderBook, OrderBookLevel
from engine.fees import round_trip_fee_pct
from engine.signal import Signal
from main import _median_reprice_hold_s, _sizing_inputs
from config.settings import settings


def make_market() -> Market:
    return Market(
        market_id="m1", question="Bitcoin Up or Down - 15 min",
        token_id_yes="tok_yes", token_id_no="tok_no",
        liquidity_usd=100_000, end_date_iso="2026-07-31T14:00:00Z",
        asset="BTC", duration_minutes=15,
    )


def make_book(bid: float, ask: float) -> OrderBook:
    return OrderBook(
        market_id="m1", token_id="tok",
        bids=(OrderBookLevel(price=bid, size=1000),),
        asks=(OrderBookLevel(price=ask, size=1000),),
    )


def make_signal(side: str, edge_pct: float) -> Signal:
    return Signal(
        market=make_market(), side=side,
        implied_prob=0.5, polymarket_prob=0.5, edge_pct=edge_pct,
        confidence=0.9, fired=True, reason="OK", model_used="fair_value",
    )


def test_sizing_uses_actual_side_price_not_yes_mid():
    """Regression for the 2026-08-12 finding: position sizing ALWAYS received
    yes_book.mid, even when buying NO — a NO entry at 0.73 was sized as if
    buying at 0.25. The fix passes the ACTUAL side's best ask."""
    yes_book = make_book(0.24, 0.26)   # YES mid = 0.25
    no_book = make_book(0.72, 0.74)    # NO best ask = 0.74
    signal = make_signal(side="NO", edge_pct=0.10)

    net_edge, entry_price = _sizing_inputs(signal, yes_book, no_book)

    assert entry_price == pytest.approx(0.74)  # NOT 0.25
    assert net_edge < 0.10  # the round-trip fee is subtracted


def test_sizing_uses_yes_ask_for_yes_buys():
    yes_book = make_book(0.24, 0.26)
    no_book = make_book(0.72, 0.74)
    signal = make_signal(side="YES", edge_pct=0.10)

    net_edge, entry_price = _sizing_inputs(signal, yes_book, no_book)

    assert entry_price == pytest.approx(0.26)


def test_sizing_subtracts_the_round_trip_fee():
    """The firing gate nets edge against round_trip_fee_pct before allowing a
    trade; sizing must use the SAME net edge, not the raw gap — otherwise
    size reflects edge that fees already consumed."""
    yes_book = make_book(0.48, 0.50)
    no_book = make_book(0.48, 0.50)
    signal = make_signal(side="YES", edge_pct=0.10)

    net_edge, entry_price = _sizing_inputs(signal, yes_book, no_book)

    expected_fee = round_trip_fee_pct(0.50, fee_rate=settings.TAKER_FEE_PCT)
    assert net_edge == pytest.approx(0.10 - expected_fee)
    assert net_edge > 0


def test_sizing_clamps_negative_net_edge_to_zero():
    """A net edge consumed entirely by fees must not produce negative size."""
    yes_book = make_book(0.48, 0.50)
    no_book = make_book(0.48, 0.50)
    signal = make_signal(side="YES", edge_pct=0.001)  # smaller than the fee

    net_edge, _ = _sizing_inputs(signal, yes_book, no_book)
    assert net_edge == 0.0


def test_median_reprice_hold_uses_closed_reprice_trades():
    trades = [
        {"exit_reason": "REPRICE", "asset": "BTC", "entry_ts": 100.0, "exit_ts": 130.0},
        {"exit_reason": "REPRICE", "asset": "BTC", "entry_ts": 200.0, "exit_ts": 260.0},
        {"exit_reason": "REPRICE", "asset": "BTC", "entry_ts": 300.0, "exit_ts": 305.0},
        {"exit_reason": "SETTLED", "asset": "BTC", "entry_ts": 400.0, "exit_ts": 500.0},
    ]
    # Holds are 30, 60, 5 -> sorted [5, 30, 60] -> median 30.
    assert _median_reprice_hold_s(trades, "BTC") == pytest.approx(30.0)
    assert _median_reprice_hold_s(trades, "ETH") is None


def test_median_reprice_hold_none_without_data():
    assert _median_reprice_hold_s([], "BTC") is None
    trades = [{"exit_reason": "SETTLED", "asset": "BTC", "entry_ts": 1.0, "exit_ts": 2.0}]
    assert _median_reprice_hold_s(trades, "BTC") is None
