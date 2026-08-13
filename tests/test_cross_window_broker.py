"""
Tests for PaperBroker.place_cross_window_order — the second risk-free leg
(same-endTime 5m/15m windows, UP on the lower beat + DOWN on the higher
beat). Mirrors the sum-to-one execution guarantees: equal-share sizing, both
legs opened as one combo, balance debited, and a fill whose combined cost
drifts >= $1 is reversed (or held) per the reverse-vs-hold decision — never
left as a naked directional position.
"""
import pytest

from data.polymarket_feed import Market, OrderBook, OrderBookLevel
from engine.broker_paper import (
    CrossWindowEdgeLostError,
    InsufficientBalanceError,
    PaperBroker,
)
from engine.cross_window import find_cross_window_opportunity, find_cross_window_pair
from storage.db import Database

T_END = 1_800_000_000.0


class FakeFeed:
    def __init__(self, books: dict[str, OrderBook]):
        self._books = books

    async def get_order_book(self, market_id: str, token_id: str) -> OrderBook:
        return self._books[token_id]

    async def get_market_outcome(self, market_id: str) -> str | None:
        return None


class MovingFeed:
    """Books change AFTER the first calls per token: the pre-place quote reads
    the GOOD book, the fill (and any reversal quote) reads the BAD book —
    mirroring the live failure where the decision saw a < $1 pair and the
    fills landed >= $1 after the book moved during the fill latency."""

    def __init__(self, good: dict[str, OrderBook], bad: dict[str, OrderBook]):
        self._good = good
        self._bad = bad
        self._calls: dict[str, int] = {}

    async def get_order_book(self, market_id: str, token_id: str) -> OrderBook:
        self._calls[token_id] = self._calls.get(token_id, 0) + 1
        if self._calls[token_id] <= 1:
            return self._good[token_id]
        return self._bad[token_id]

    async def get_market_outcome(self, market_id: str) -> str | None:
        return None


def make_window(market_id: str, duration: int, reference: float) -> Market:
    open_ts = T_END - duration * 60
    # No category: the broker falls back to the configured fee_pct, so tests
    # can use fee_pct=0.0 to isolate execution from fee math (the sum-to-one
    # broker tests use the same convention).
    return Market(
        market_id=market_id,
        question=f"Bitcoin Up or Down - {duration} min",
        token_id_yes=f"{market_id}_yes", token_id_no=f"{market_id}_no",
        liquidity_usd=100_000, end_date_iso="2026-08-13T14:00:00Z",
        asset="BTC", duration_minutes=duration,
        reference_price=reference,
        reference_captured_at=open_ts + 2.0,
        expires_at_ts=T_END,
    )


def make_book(token_id: str, best_bid: float, best_ask: float, size=50_000) -> OrderBook:
    return OrderBook(
        market_id="x", token_id=token_id,
        bids=(OrderBookLevel(price=best_bid, size=size),),
        asks=(OrderBookLevel(price=best_ask, size=size),),
    )


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    yield database
    await database.close()


async def test_place_cross_window_order_buys_both_legs(db):
    m5 = make_window("m5", 5, reference=64_000)    # lower beat -> UP
    m15 = make_window("m15", 15, reference=65_000)  # higher beat -> DOWN
    books = {
        m5.token_id_yes: make_book(m5.token_id_yes, 0.44, 0.46),
        m15.token_id_no: make_book(m15.token_id_no, 0.46, 0.48),
    }
    broker = PaperBroker(db=db, feed=FakeFeed(books), starting_balance_usd=1000, fee_pct=0.0)

    pair = find_cross_window_pair([m5, m15], max_ref_capture_delay_s=10.0, min_beat_gap_pct=0.0001)
    assert pair == (m5, m15)
    opp = find_cross_window_opportunity(
        m5, m15, books[m5.token_id_yes], books[m15.token_id_no],
        min_edge_pct=0.01, fee_pct=0.0,
    )
    assert opp is not None

    fill_a, fill_b = await broker.place_cross_window_order(opp, total_size_usd=100)

    assert fill_a.side == "YES" and fill_a.market_id == "m5"
    assert fill_b.side == "NO" and fill_b.market_id == "m15"
    # Equal-share sizing: BOTH legs carry the SAME share count.
    assert fill_a.shares == pytest.approx(fill_b.shares)
    open_trades = await db.get_open_trades(mode="PAPER")
    assert len(open_trades) == 2
    assert len({t["combo_group_id"] for t in open_trades}) == 1
    assert None not in {t["combo_group_id"] for t in open_trades}
    assert {t["strategy"] for t in open_trades} == {"cross_window"}
    # Balance debited for exactly what the two legs cost (sizes + fees).
    expected_cost = (
        fill_a.size_usd + fill_b.size_usd + fill_a.fee_usd + fill_b.fee_usd
    )
    assert broker.balance_usd == pytest.approx(1000 - expected_cost, abs=0.01)


async def test_cross_window_refuses_when_budget_below_one_pair(db):
    m5 = make_window("m5", 5, reference=64_000)
    m15 = make_window("m15", 15, reference=65_000)
    books = {
        m5.token_id_yes: make_book(m5.token_id_yes, 0.44, 0.46),
        m15.token_id_no: make_book(m15.token_id_no, 0.46, 0.48),
    }
    broker = PaperBroker(db=db, feed=FakeFeed(books), starting_balance_usd=0.5, fee_pct=0.0)
    opp = find_cross_window_opportunity(
        m5, m15, books[m5.token_id_yes], books[m15.token_id_no],
        min_edge_pct=0.01, fee_pct=0.0,
    )
    assert opp is not None
    with pytest.raises(InsufficientBalanceError):
        await broker.place_cross_window_order(opp, total_size_usd=100)
    assert not broker.has_open_position("m5")
    assert not broker.has_open_position("m15")
    assert await db.get_open_trades(mode="PAPER") == []


async def test_cross_window_edge_lost_at_fill_reverses_both_legs(db):
    """The decision read a < $1 pair; the book moved during the fill latency
    and the fills landed >= $1. Both legs must be reversed (never left as a
    half-open hedge), and nothing may remain open."""
    m5 = make_window("m5", 5, reference=64_000)
    m15 = make_window("m15", 15, reference=65_000)
    good = {
        m5.token_id_yes: make_book(m5.token_id_yes, 0.44, 0.46),
        m15.token_id_no: make_book(m15.token_id_no, 0.46, 0.48),
    }
    # Fills land at 0.70 + 0.50 = 1.20 (a guaranteed loss). Bids sit close to
    # the fills so reversing both legs loses strictly LESS than holding to
    # settlement's worst case (only one leg pays $1) — the broker reverses.
    bad = {
        m5.token_id_yes: make_book(m5.token_id_yes, 0.68, 0.70),
        m15.token_id_no: make_book(m15.token_id_no, 0.49, 0.50),
    }
    broker = PaperBroker(
        db=db, feed=MovingFeed(good, bad), starting_balance_usd=1000,
        fee_pct=0.0, simulated_fill_latency_s=0.3,
    )
    opp = find_cross_window_opportunity(
        m5, m15, good[m5.token_id_yes], good[m15.token_id_no],
        min_edge_pct=0.01, fee_pct=0.0,
    )
    assert opp is not None

    with pytest.raises(CrossWindowEdgeLostError) as excinfo:
        await broker.place_cross_window_order(opp, total_size_usd=100)
    assert "both legs reversed" in str(excinfo.value)

    assert not broker.has_open_position("m5")
    assert not broker.has_open_position("m15")
    assert await db.get_open_trades(mode="PAPER") == []
