"""
Tests for the PaperBroker functionality added to fix the restart/settlement/
equity/exposure bugs identified in code review: load_open_positions(),
get_equity(), get_total_exposure_usd(), place_sum_to_one_order(),
close_position_early(), and min-order-size/tick-size enforcement.
"""
import pytest

from data.polymarket_feed import Market, OrderBook, OrderBookLevel
from engine.broker_paper import OrderTooSmallError, PaperBroker
from engine.sum_to_one import find_sum_to_one_opportunity
from storage.db import Database


class FakeFeed:
    """Stand-in for PolymarketFeed. Supports per-token books so YES/NO can differ."""

    def __init__(self, books: dict[str, OrderBook], outcome: str | None = None):
        self._books = books  # token_id -> OrderBook
        self._outcome = outcome

    async def get_order_book(self, market_id: str, token_id: str) -> OrderBook:
        return self._books[token_id]

    async def get_market_outcome(self, market_id: str) -> str | None:
        return self._outcome


def make_market(market_id="m1") -> Market:
    return Market(
        market_id=market_id, question="Bitcoin Up or Down - 15 min",
        token_id_yes="tok_yes", token_id_no="tok_no",
        liquidity_usd=100_000, end_date_iso="2026-07-31T14:00:00Z",
        asset="BTC", duration_minutes=15,
    )


def make_symmetric_book(token_id: str, best_bid=0.54, best_ask=0.56) -> OrderBook:
    return OrderBook(
        market_id="m1", token_id=token_id,
        bids=(OrderBookLevel(price=best_bid, size=1000),),
        asks=(OrderBookLevel(price=best_ask, size=1000),),
    )


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    yield database
    await database.close()


# -- restart recovery ---------------------------------------------------------

async def test_load_open_positions_restores_after_restart(db):
    books = {"tok_yes": make_symmetric_book("tok_yes"), "tok_no": make_symmetric_book("tok_no")}
    feed = FakeFeed(books)
    market = make_market()

    broker1 = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0)
    await broker1.place_order(market, "YES", 100)
    assert broker1.has_open_position("m1")

    # Simulate a restart: a fresh broker instance sharing the same DB.
    broker2 = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0)
    assert not broker2.has_open_position("m1")  # nothing loaded yet

    restored_count = await broker2.load_open_positions()
    assert restored_count == 1
    assert broker2.has_open_position("m1")


async def test_restart_reconstructs_balance_from_trade_ledger(db):
    """Regression guard (2026-08-06): the paper balance is an in-memory float
    that reset to the starting balance on every restart, so a restart with
    open positions re-counted money that was already spent — the equity curve
    visibly jumped UP ~$43 on a real restart. load_open_positions() must
    reconstruct cash from the TRADE LEDGER (starting balance + closed PnL -
    open costs), not from the corrupted equity curve."""
    books = {"tok_yes": make_symmetric_book("tok_yes"), "tok_no": make_symmetric_book("tok_no")}
    feed = FakeFeed(books)
    market = make_market()

    broker1 = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0)
    await broker1.place_order(market, "YES", 100)  # balance now 900

    # Restart: fresh broker with the same DB, but poison the equity curve to
    # prove the ledger (not the curve) is the source of truth.
    await db.record_equity(mode="PAPER", balance_usd=9999.0)

    broker2 = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0)
    await broker2.load_open_positions()
    assert await broker2.get_balance() == pytest.approx(900)


async def test_load_open_positions_is_idempotent(db):
    """Regression guard: balance reconstruction must start from the fixed
    starting-balance baseline captured at construction, so calling
    load_open_positions() twice never double-counts closed PnL or open costs."""
    books = {"tok_yes": make_symmetric_book("tok_yes"), "tok_no": make_symmetric_book("tok_no")}
    feed = FakeFeed(books)
    market = make_market()

    broker1 = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0)
    await broker1.place_order(market, "YES", 100)

    broker2 = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0)
    await broker2.load_open_positions()
    first = await broker2.get_balance()
    await broker2.load_open_positions()  # second call must not re-add/subtract
    assert await broker2.get_balance() == pytest.approx(first)


async def test_restart_balance_includes_closed_pnl(db):
    """A closed winner must be added back into the reconstructed balance on a
    restart, not just the open-position costs subtracted."""
    books = {"tok_yes": make_symmetric_book("tok_yes"), "tok_no": make_symmetric_book("tok_no")}
    feed = FakeFeed(books)
    market = make_market()

    broker1 = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0)
    fill = await broker1.place_order(market, "YES", 100)
    # Close it as a winner at 0.60 vs 0.56 entry.
    books["tok_yes"] = make_symmetric_book("tok_yes", best_bid=0.60, best_ask=0.62)
    await broker1.close_position_early(market, fill.trade_id, reason="TAKE_PROFIT")

    broker2 = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0)
    await broker2.load_open_positions()
    # 1000 - 100 (cost) + (0.60/0.56*100 = 107.14 proceeds, fee 0) = 1007.14
    assert await broker2.get_balance() == pytest.approx(1007.142857, rel=1e-3)


async def test_settle_position_works_after_restart_recovery(db):
    books = {"tok_yes": make_symmetric_book("tok_yes"), "tok_no": make_symmetric_book("tok_no")}
    market = make_market()

    broker1 = PaperBroker(db=db, feed=FakeFeed(books), starting_balance_usd=1000, fee_pct=0.0)
    await broker1.place_order(market, "YES", 100)

    # New broker instance + feed with the resolved outcome now available.
    feed2 = FakeFeed(books, outcome="YES")
    broker2 = PaperBroker(db=db, feed=feed2, starting_balance_usd=1000, fee_pct=0.0)
    await broker2.load_open_positions()

    pnl = await broker2.settle_position(market)
    assert pnl is not None
    assert pnl > 0


# -- true equity vs cash --------------------------------------------------------

async def test_equity_includes_unrealized_position_value(db):
    books = {"tok_yes": make_symmetric_book("tok_yes", best_bid=0.54, best_ask=0.56),
             "tok_no": make_symmetric_book("tok_no")}
    feed = FakeFeed(books)
    market = make_market()

    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0)
    await broker.place_order(market, "YES", 100)

    cash = await broker.get_balance()
    equity = await broker.get_equity(known_markets={"m1": market})

    assert cash == pytest.approx(900)  # $100 stake deducted from cash
    # Equity should reflect the position's current mark-to-market value, not
    # just leftover cash — with the book unchanged, equity should be close to
    # the original $1000 (mark near entry price), clearly more than raw cash.
    assert equity > cash
    assert equity == pytest.approx(1000, rel=0.05)


async def test_equity_falls_back_to_cost_basis_for_unknown_market(db):
    books = {"tok_yes": make_symmetric_book("tok_yes"), "tok_no": make_symmetric_book("tok_no")}
    feed = FakeFeed(books)
    market = make_market()

    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0)
    await broker.place_order(market, "YES", 100)

    # known_markets doesn't include "m1" -- should not raise, should fall back.
    equity = await broker.get_equity(known_markets={})
    assert equity == pytest.approx(1000)  # 900 cash + 100 cost-basis fallback


# -- exposure tracking -----------------------------------------------------------

async def test_total_exposure_sums_open_positions(db):
    books = {"tok_yes": make_symmetric_book("tok_yes"), "tok_no": make_symmetric_book("tok_no")}
    feed = FakeFeed(books)
    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0)

    await broker.place_order(make_market("m1"), "YES", 100)
    await broker.place_order(make_market("m2"), "YES", 50)

    exposure = await broker.get_total_exposure_usd()
    assert exposure == pytest.approx(150)


# -- sum-to-one execution --------------------------------------------------------

async def test_place_sum_to_one_order_buys_both_legs(db):
    books = {"tok_yes": make_symmetric_book("tok_yes", best_ask=0.46),
             "tok_no": make_symmetric_book("tok_no", best_ask=0.48)}
    feed = FakeFeed(books)
    market = make_market()
    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0)

    opp = find_sum_to_one_opportunity(
        market,
        yes_book=books["tok_yes"], no_book=books["tok_no"],
        min_edge_pct=0.01, fee_pct=0.0,
    )
    assert opp is not None

    yes_fill, no_fill = await broker.place_sum_to_one_order(opp, total_size_usd=100)

    assert yes_fill.side == "YES"
    assert no_fill.side == "NO"
    # Both legs share a combo_group_id, linking them as one risk-free position.
    open_trades = await db.get_open_trades(mode="PAPER")
    combo_ids = {t["combo_group_id"] for t in open_trades}
    assert len(combo_ids) == 1
    assert None not in combo_ids


async def test_sum_to_one_settlement_guarantees_profit_regardless_of_outcome(db):
    books = {"tok_yes": make_symmetric_book("tok_yes", best_ask=0.46),
             "tok_no": make_symmetric_book("tok_no", best_ask=0.48)}
    market = make_market()

    broker1 = PaperBroker(db=db, feed=FakeFeed(books), starting_balance_usd=1000, fee_pct=0.0)
    opp = find_sum_to_one_opportunity(market, books["tok_yes"], books["tok_no"], min_edge_pct=0.01, fee_pct=0.0)
    await broker1.place_sum_to_one_order(opp, total_size_usd=100)
    balance_after_entry = broker1.balance_usd

    # Resolve YES -- the sum-to-one pair should still show a net profit
    # despite the NO leg losing, because 0.46+0.48=0.94 < $1.
    feed2 = FakeFeed(books, outcome="YES")
    broker2 = PaperBroker(db=db, feed=feed2, starting_balance_usd=1000, fee_pct=0.0)
    await broker2.load_open_positions()
    total_pnl = await broker2.settle_position(market)

    assert total_pnl is not None
    assert total_pnl > 0  # guaranteed profit regardless of which side won


# -- early exit ---------------------------------------------------------------

async def test_close_position_early_realizes_pnl_before_settlement(db):
    books = {"tok_yes": make_symmetric_book("tok_yes", best_bid=0.54, best_ask=0.56),
             "tok_no": make_symmetric_book("tok_no")}
    feed = FakeFeed(books)
    market = make_market()

    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0)
    fill = await broker.place_order(market, "YES", 100)  # entry @ 0.56 (ask)
    assert fill.avg_price == pytest.approx(0.56)

    # Simulate the book moving up favorably before we exit (a real repricing,
    # not just the natural bid-ask spread cost).
    books["tok_yes"] = make_symmetric_book("tok_yes", best_bid=0.70, best_ask=0.72)

    pnl = await broker.close_position_early(market, fill.trade_id, reason="TAKE_PROFIT")

    assert pnl is not None
    assert pnl > 0  # exited at the new 0.70 bid, well above the 0.56 entry
    assert not broker.has_open_position("m1")


async def test_close_position_early_can_show_a_loss_from_spread_alone(db):
    """With no book movement at all, exiting immediately after entry realizes
    a loss purely from crossing the bid-ask spread -- this is expected and
    is exactly the kind of cost paper mode should surface, not hide."""
    books = {"tok_yes": make_symmetric_book("tok_yes", best_bid=0.54, best_ask=0.56),
             "tok_no": make_symmetric_book("tok_no")}
    feed = FakeFeed(books)
    market = make_market()

    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0)
    fill = await broker.place_order(market, "YES", 100)

    pnl = await broker.close_position_early(market, fill.trade_id, reason="TAKE_PROFIT")
    assert pnl is not None
    assert pnl < 0  # bought at 0.56, sold at 0.54 -- the spread itself is a real cost


# -- min order size / tick size --------------------------------------------------

async def test_order_below_minimum_size_rejected(db):
    books = {"tok_yes": make_symmetric_book("tok_yes"), "tok_no": make_symmetric_book("tok_no")}
    feed = FakeFeed(books)
    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0, min_order_size_usd=5.0)

    with pytest.raises(OrderTooSmallError):
        await broker.place_order(make_market(), "YES", 1.0)


async def test_fill_price_rounded_to_tick_size(db):
    # best_ask=0.567 with tick_size=0.01 should round the fill to 0.57.
    books = {"tok_yes": OrderBook(market_id="m1", token_id="tok_yes", bids=(), asks=(OrderBookLevel(price=0.567, size=1000),)),
             "tok_no": make_symmetric_book("tok_no")}
    feed = FakeFeed(books)
    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0, tick_size=0.01)

    fill = await broker.place_order(make_market(), "YES", 100)
    assert fill.avg_price == pytest.approx(0.57)
