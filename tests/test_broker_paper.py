"""
Tests for engine/broker_paper.py. Uses a fake feed returning fixture order
books rather than a live PolymarketFeed — no network calls.
"""
import pytest

from data.polymarket_feed import Market, OrderBook, OrderBookLevel
from engine.broker_paper import InsufficientBalanceError, PaperBroker
from storage.db import Database


class FakeFeed:
    """Minimal stand-in for PolymarketFeed exposing only what PaperBroker needs."""

    def __init__(self, book: OrderBook, outcome: str | None = None):
        self._book = book
        self._outcome = outcome

    async def get_order_book(self, market_id: str, token_id: str) -> OrderBook:
        return self._book

    async def get_market_outcome(self, market_id: str) -> str | None:
        return self._outcome


def make_market() -> Market:
    return Market(
        market_id="m1",
        question="Bitcoin Up or Down - 15 min",
        token_id_yes="tok_yes",
        token_id_no="tok_no",
        liquidity_usd=100_000,
        end_date_iso="2026-07-31T14:00:00Z",
        asset="BTC",
        duration_minutes=15,
    )


def make_book() -> OrderBook:
    return OrderBook(
        market_id="m1",
        token_id="tok_yes",
        bids=(OrderBookLevel(price=0.54, size=1000),),
        asks=(
            OrderBookLevel(price=0.56, size=100),   # thin first level -> forces walking the book
            OrderBookLevel(price=0.58, size=1000),
        ),
    )


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    yield database
    await database.close()


async def test_place_order_walks_multiple_book_levels(db):
    feed = FakeFeed(make_book())
    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0)
    market = make_market()

    # $100 requested: first level only has 100*0.56=$56 available, remainder
    # must be filled from the second level at 0.58.
    fill = await broker.place_order(market, "YES", size_usd=100)

    assert fill.avg_price > 0.56  # blended price should be worse than best ask alone
    assert fill.avg_price < 0.58
    assert fill.size_usd == pytest.approx(100)


async def test_place_order_rejects_insufficient_balance(db):
    feed = FakeFeed(make_book())
    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=50, fee_pct=0.0)
    market = make_market()

    with pytest.raises(InsufficientBalanceError):
        await broker.place_order(market, "YES", size_usd=100)


async def test_place_order_deducts_fee_from_balance(db):
    feed = FakeFeed(make_book())
    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.02)
    market = make_market()

    await broker.place_order(market, "YES", size_usd=100)

    # $100 size + 2% fee ($2) = $102 deducted
    assert broker.balance_usd == pytest.approx(1000 - 102)


async def test_insufficient_depth_raises_value_error(db):
    thin_book = OrderBook(
        market_id="m1", token_id="tok_yes",
        bids=(OrderBookLevel(price=0.54, size=10),),
        asks=(OrderBookLevel(price=0.56, size=10),),  # only $5.60 of depth
    )
    feed = FakeFeed(thin_book)
    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0)
    market = make_market()

    with pytest.raises(ValueError):
        await broker.place_order(market, "YES", size_usd=100)


async def test_settle_position_wins_pays_out_full_shares(db):
    feed = FakeFeed(make_book(), outcome="YES")
    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0)
    market = make_market()

    fill = await broker.place_order(market, "YES", size_usd=100)
    balance_after_entry = broker.balance_usd

    pnl = await broker.settle_position(market)

    assert pnl is not None
    assert pnl > 0  # won: payout (shares * $1) exceeds the $100 staked
    assert broker.balance_usd > balance_after_entry


async def test_settle_position_loses_pays_out_zero(db):
    feed = FakeFeed(make_book(), outcome="NO")
    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0)
    market = make_market()

    await broker.place_order(market, "YES", size_usd=100)
    pnl = await broker.settle_position(market)

    assert pnl == pytest.approx(-100)  # lost full stake, no fee in this test


# -- cancel stubs (interface parity with LiveBroker) -------------------------


async def test_cancel_order_stub_returns_true(db):
    """Paper mode has no resting orders — cancel_order always reports success."""
    feed = FakeFeed(make_book())
    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0)

    assert await broker.cancel_order("any-order-id") is True


async def test_cancel_all_orders_stub_returns_zero(db):
    """No resting orders in paper mode — cancel_all_orders returns 0, with or
    without a market scope."""
    feed = FakeFeed(make_book())
    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0)

    assert await broker.cancel_all_orders() == 0
    assert await broker.cancel_all_orders(market_id="m1") == 0
