"""
Tests for the sum-to-one MAKER execution path in engine/broker_paper.py
(added 2026-08-12): post the cheaper leg as a resting buy at the bid (zero
fee + strictly cheaper price), and the instant it fills take the other leg at
market. These tests use a fake feed with per-token books — no network calls.
"""
import pytest

from data.polymarket_feed import Market, OrderBook, OrderBookLevel
from engine.broker_paper import PaperBroker
from storage.db import Database


class FakeFeed:
    """Per-token books, mutable so tests can evolve the book between cycles."""

    def __init__(self, books: dict[str, OrderBook], outcome: str | None = None):
        self._books = books
        self._outcome = outcome

    async def get_order_book(self, market_id: str, token_id: str) -> OrderBook:
        return self._books[token_id]

    async def get_market_outcome(self, market_id: str) -> str | None:
        return self._outcome


def book(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> OrderBook:
    return OrderBook(
        market_id="m1", token_id="tok",
        bids=tuple(OrderBookLevel(p, s) for p, s in bids),
        asks=tuple(OrderBookLevel(p, s) for p, s in asks),
    )


def make_market() -> Market:
    return Market(
        market_id="m1", question="Bitcoin Up or Down - 15 min",
        token_id_yes="tok_yes", token_id_no="tok_no",
        liquidity_usd=100_000, end_date_iso="2026-07-31T14:00:00Z",
        asset="BTC", duration_minutes=15,
    )


# Default books: YES is the CHEAP side (ask 0.45 < NO ask 0.53). Posting the
# YES leg as a maker at its bid 0.42 locks 1 - 0.42 - 0.53 = 5% before fees.
def default_books() -> dict[str, OrderBook]:
    return {
        "tok_yes": book([(0.42, 2000)], [(0.45, 2000)]),
        "tok_no": book([(0.50, 2000)], [(0.53, 2000)]),
    }


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def broker(db):
    feed = FakeFeed(default_books())
    return PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0)


# -- post -------------------------------------------------------------------


async def test_post_picks_cheap_side_and_reserves_cash(broker):
    order = await broker.post_sum_to_one_maker(make_market(), total_size_usd=100)

    assert order is not None
    assert order.maker_side == "YES"   # cheaper side (0.45 < 0.53)
    assert order.taker_side == "NO"
    assert order.maker_price == pytest.approx(0.42)  # resting at the bid
    assert order.half_size_usd == pytest.approx(50)
    assert order.status == "PENDING"
    assert broker.balance_usd == pytest.approx(900)  # 100 reserved
    assert broker.has_pending_maker("m1") is True
    # Exposure accounting must include the committed reservation.
    assert await broker.get_total_exposure_usd() == pytest.approx(100)

    rows = await broker.db.get_maker_orders(status="PENDING")
    assert len(rows) == 1
    assert rows[0]["price"] == pytest.approx(0.42)
    assert rows[0]["size_usd"] == pytest.approx(50)


async def test_post_refused_when_maker_combo_does_not_lock(broker):
    # Maker at 0.50 + taker at 0.52 = 1.02 — no profit to lock.
    broker.feed._books = {
        "tok_yes": book([(0.50, 2000)], [(0.52, 2000)]),
        "tok_no": book([(0.50, 2000)], [(0.52, 2000)]),
    }
    order = await broker.post_sum_to_one_maker(make_market(), total_size_usd=100)
    assert order is None
    assert broker.balance_usd == pytest.approx(1000)  # nothing reserved


async def test_post_refused_when_taker_book_too_thin(broker):
    # The pair locks, but the taker leg's ask side can't absorb our size —
    # an oversized maker fill could never be paired.
    broker.feed._books = {
        "tok_yes": book([(0.42, 2000)], [(0.45, 2000)]),
        "tok_no": book([(0.50, 200)], [(0.53, 10)]),  # only $5.30 of ask depth
    }
    order = await broker.post_sum_to_one_maker(make_market(), total_size_usd=100)
    assert order is None
    assert broker.balance_usd == pytest.approx(1000)


async def test_post_refused_when_no_bid_on_cheap_side(broker):
    broker.feed._books = {
        "tok_yes": book([], [(0.45, 2000)]),           # no bids at all
        "tok_no": book([(0.50, 2000)], [(0.53, 2000)]),
    }
    order = await broker.post_sum_to_one_maker(make_market(), total_size_usd=100)
    assert order is None


async def test_post_refused_when_insufficient_balance(broker):
    broker.balance_usd = 50  # pair of $100 can't be reserved
    order = await broker.post_sum_to_one_maker(make_market(), total_size_usd=100)
    assert order is None


# -- fill -> pair -----------------------------------------------------------


async def test_fill_pairs_both_legs_with_zero_fee_maker_leg(broker):
    order = await broker.post_sum_to_one_maker(make_market(), total_size_usd=100)
    assert order is not None

    # Sellers cross down to our resting bid: YES asks now at 0.42.
    broker.feed._books["tok_yes"] = book([(0.40, 2000)], [(0.42, 200)])
    actions = await broker.check_sum_to_one_makers()

    assert actions and actions[0].startswith("maker_paired")
    assert broker.has_pending_maker("m1") is False

    trades = await broker.db.get_open_trades(mode="PAPER")
    assert len(trades) == 2
    yes_trade = next(t for t in trades if t["side"] == "YES")
    no_trade = next(t for t in trades if t["side"] == "NO")
    # Maker leg: entered at the resting BID with ZERO fee; taker leg at the ask.
    assert yes_trade["entry_price"] == pytest.approx(0.42)
    assert yes_trade["fee_usd"] == pytest.approx(0.0)
    assert no_trade["entry_price"] == pytest.approx(0.53)
    assert no_trade["strategy"] == "sum_to_one"
    assert yes_trade["combo_group_id"] == no_trade["combo_group_id"]

    rows = await broker.db.get_maker_orders()
    assert len(rows) == 1
    assert rows[0]["status"] == "FILLED"
    assert rows[0]["filled_price"] == pytest.approx(0.42)
    assert rows[0]["combined_cost"] == pytest.approx(0.95)


async def test_fill_pairs_only_filled_portion(broker):
    """A PARTIAL maker fill pairs only the filled portion — never more."""
    order = await broker.post_sum_to_one_maker(make_market(), total_size_usd=100)
    assert order is not None

    # Only $21 of ask depth crosses to our bid (0.42 * 50 shares).
    broker.feed._books["tok_yes"] = book([(0.40, 2000)], [(0.42, 50)])
    actions = await broker.check_sum_to_one_makers()
    assert actions and actions[0].startswith("maker_paired")

    trades = await broker.db.get_open_trades(mode="PAPER")
    assert len(trades) == 2
    assert trades[0]["size_usd"] == pytest.approx(21)   # 50 shares * 0.42
    assert trades[1]["size_usd"] == pytest.approx(21)


# -- fill -> lock broken -> reverse -----------------------------------------


async def test_fill_with_broken_lock_reverses_maker_leg(broker):
    order = await broker.post_sum_to_one_maker(make_market(), total_size_usd=100)
    assert order is not None

    # Maker fills, but the taker leg has walked up to 0.60: 0.42 + 0.60 > 1 —
    # taking it would turn a guaranteed profit into a guaranteed loss.
    broker.feed._books["tok_yes"] = book([(0.40, 2000)], [(0.42, 200)])
    broker.feed._books["tok_no"] = book([(0.50, 2000)], [(0.60, 2000)])
    actions = await broker.check_sum_to_one_makers()

    assert actions and actions[0].startswith("maker_reversed")
    assert broker.has_pending_maker("m1") is False
    # No positions were opened — the pair never became a trade.
    assert await broker.db.get_open_trades(mode="PAPER") == []

    # Cash: reserved 100 at post, refunded, then maker P&L = -50 (fill) + sold
    # 119.05 shares @ 0.40 = 47.62 (no fee) -> balance 997.62.
    assert broker.balance_usd == pytest.approx(1000 - 100 + 100 - 50 + 50 / 0.42 * 0.40, abs=0.01)

    rows = await broker.db.get_maker_orders()
    assert rows[0]["status"] == "REVERSED"
    assert "lock broken" in rows[0]["notes"]


async def test_fill_with_no_bid_depth_rides_to_settlement(broker):
    order = await broker.post_sum_to_one_maker(make_market(), total_size_usd=100)
    assert order is not None

    # Maker fills; the maker side now has NO bid depth to sell into.
    broker.feed._books["tok_yes"] = book([], [(0.42, 200)])
    broker.feed._books["tok_no"] = book([(0.50, 2000)], [(0.60, 2000)])
    actions = await broker.check_sum_to_one_makers()

    assert actions and actions[0].startswith("maker_held_to_settlement")
    # The maker leg is recorded as a real (unpaired) trade so its settlement
    # P&L is realized honestly: cash = reserve refunded minus the fill cost.
    assert broker.balance_usd == pytest.approx(1000 - 100 + 100 - 50)
    trades = await broker.db.get_open_trades(mode="PAPER")
    assert len(trades) == 1
    assert trades[0]["side"] == "YES"
    assert trades[0]["size_usd"] == pytest.approx(50)
    assert trades[0]["fee_usd"] == pytest.approx(0.0)
    # And it settles normally when the market resolves.
    broker.feed._outcome = "YES"
    pnl = await broker.settle_position(make_market())
    assert pnl is not None and pnl > 0

    rows = await broker.db.get_maker_orders()
    assert rows[0]["status"] == "HELD"


# -- timeout -> cancel ------------------------------------------------------


async def test_timeout_cancels_and_refunds(broker):
    order = await broker.post_sum_to_one_maker(make_market(), total_size_usd=100)
    assert order is not None
    order.posted_at -= 10_000  # pretend it aged past the timeout

    actions = await broker.check_sum_to_one_makers()

    assert actions and actions[0].startswith("maker_cancelled_timeout")
    assert broker.balance_usd == pytest.approx(1000)  # full refund
    assert broker.has_pending_maker("m1") is False
    rows = await broker.db.get_maker_orders()
    assert rows[0]["status"] == "CANCELLED"


async def test_still_pending_without_fill(broker):
    await broker.post_sum_to_one_maker(make_market(), total_size_usd=100)
    actions = await broker.check_sum_to_one_makers()  # book unchanged: no crossing
    assert actions == []
    assert broker.has_pending_maker("m1") is True
    assert broker.balance_usd == pytest.approx(900)


# -- fees -------------------------------------------------------------------


async def test_maker_leg_pays_zero_fee_and_reservation_matches(db):
    """With the real 0.07 rate: the maker leg's fee is zero, the taker leg
    pays rate * (1 - p) of its notional, and the reservation covers exactly
    the actual cost (no phantom refund)."""
    feed = FakeFeed(default_books())
    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.07)

    order = await broker.post_sum_to_one_maker(make_market(), total_size_usd=100)
    assert order is not None
    # est taker fee = 50 * 0.07 * (1 - 0.53) = 1.645
    assert broker.balance_usd == pytest.approx(1000 - 101.645, abs=0.001)

    broker.feed._books["tok_yes"] = book([(0.40, 2000)], [(0.42, 200)])
    await broker.check_sum_to_one_makers()

    trades = await broker.db.get_open_trades(mode="PAPER")
    yes_trade = next(t for t in trades if t["side"] == "YES")
    no_trade = next(t for t in trades if t["side"] == "NO")
    assert yes_trade["fee_usd"] == pytest.approx(0.0)
    assert no_trade["fee_usd"] == pytest.approx(50 * 0.07 * (1 - 0.53), abs=0.001)
    # Balance exactly unchanged from post: reservation == actual cost.
    assert broker.balance_usd == pytest.approx(1000 - 101.645, abs=0.001)


# -- restart cleanup --------------------------------------------------------


async def test_load_open_positions_cancels_stale_pending_rows(db):
    """A PENDING maker order cannot survive a restart (the registry is
    rebuilt empty) — load_open_positions must mark stale rows CANCELLED so
    the audit trail shows no phantom open resting orders."""
    await db.log_maker_order(
        market_id="m1", side="YES", token_id="tok_yes",
        price=0.42, size_usd=50, notes="left over from a crashed run",
    )
    feed = FakeFeed(default_books())
    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0)
    await broker.load_open_positions()

    rows = await db.get_maker_orders()
    assert len(rows) == 1
    assert rows[0]["status"] == "CANCELLED"
    assert "restart" in rows[0]["notes"]
    assert broker.has_pending_maker("m1") is False
