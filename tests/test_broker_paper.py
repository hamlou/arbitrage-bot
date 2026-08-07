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
    """The fee is Polymarket's price-dependent schedule (rate * (1 - p) of
    notional, i.e. rate * p * (1 - p) per share), NOT a flat 2% — at the
    blended ~0.569 fill price that's ~0.86% of size with fee_rate=0.02."""
    from engine.fees import taker_fee_fraction_of_notional

    feed = FakeFeed(make_book())
    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.02)
    market = make_market()

    fill = await broker.place_order(market, "YES", size_usd=100)

    expected_fee = 100 * taker_fee_fraction_of_notional(fill.avg_price, fee_rate=0.02)
    assert broker.balance_usd == pytest.approx(1000 - 100 - expected_fee)
    assert expected_fee < 2.0  # sanity: well under the old flat 2% at this price


async def test_place_order_persists_fill_realism_metrics(db):
    """Slippage + decision/fill best asks must be stored on the trade row —
    the paper-mode 'how much edge did we lose between deciding and filling'
    metric. Verified 2026-08-07: the broker computed these and threw them
    away, so the metric was unanswerable from the DB."""
    feed = FakeFeed(make_book())
    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0)

    fill = await broker.place_order(make_market(), "YES", size_usd=100)

    trades = await db.get_all_trades(mode="PAPER")
    assert len(trades) == 1
    t = trades[0]
    assert t["decision_best_ask"] == pytest.approx(0.56)
    assert t["fill_best_ask"] == pytest.approx(0.56)
    assert t["slippage_pct"] == pytest.approx(fill.slippage_pct)
    assert fill.slippage_pct > 0  # walking the book into 0.58 must cost something


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


async def test_existing_db_migrates_new_fill_columns(tmp_path):
    """A DB created before the fill-realism columns existed must get them via
    the column migration on connect — otherwise open_trade's new 3-column
    INSERT would throw on the running bot's existing DB at the worst moment
    (a restart). Reviewed 2026-08-07: only the fresh-DB path was covered."""
    path = tmp_path / "old.db"
    db = Database(str(path))
    await db.connect()
    # Simulate an old-schema DB: drop the columns schema.sql now creates.
    await db._conn.execute("ALTER TABLE trades DROP COLUMN slippage_pct")
    await db._conn.execute("ALTER TABLE trades DROP COLUMN decision_best_ask")
    await db._conn.execute("ALTER TABLE trades DROP COLUMN fill_best_ask")
    await db._conn.commit()
    await db.close()

    db2 = Database(str(path))
    await db2.connect()  # _migrate_missing_columns must re-add the columns
    trade_id = await db2.open_trade(
        signal_id=None, market_id="m1", asset="BTC", side="YES", mode="PAPER",
        entry_price=0.55, size_usd=100, fee_usd=2.0,
        slippage_pct=0.01, decision_best_ask=0.56, fill_best_ask=0.57,
    )
    assert trade_id > 0
    trades = await db2.get_all_trades(mode="PAPER")
    assert trades[0]["slippage_pct"] == pytest.approx(0.01)
    assert trades[0]["decision_best_ask"] == pytest.approx(0.56)
    await db2.close()


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
