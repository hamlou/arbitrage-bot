"""
Tests for the PaperBroker functionality added to fix the restart/settlement/
equity/exposure bugs identified in code review: load_open_positions(),
get_equity(), get_total_exposure_usd(), place_sum_to_one_order(),
close_position_early(), and min-order-size/tick-size enforcement.
"""
import pytest

from data.polymarket_feed import Market, OrderBook, OrderBookLevel
from engine.broker_paper import (
    EntryPriceExceededError,
    InsufficientBalanceError,
    OrderTooSmallError,
    PaperBroker,
    SumToOneEdgeLostError,
)
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


class MovingFeed:
    """FakeFeed variant whose books CHANGE after the first calls per token:
    the pre-place quote and the decision read the GOOD book; the fill (and a
    later reversal quote) read the BAD book. Mirrors the live 2026-08-07/09
    failures where the decision saw a < $1 combo and the fills landed at
    >= $1 because the book moved during the fill latency."""

    def __init__(self, good: dict[str, OrderBook], bad: dict[str, OrderBook], good_for_first_n: int = 2):
        self._good = good
        self._bad = bad
        self._good_for_first_n = good_for_first_n
        self._calls: dict[str, int] = {}

    async def get_order_book(self, market_id: str, token_id: str) -> OrderBook:
        self._calls[token_id] = self._calls.get(token_id, 0) + 1
        if self._calls[token_id] <= self._good_for_first_n:
            return self._good[token_id]
        return self._bad[token_id]

    async def get_market_outcome(self, market_id: str) -> str | None:
        return None


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


# -- entry-price cap on the real fill ----------------------------------------

async def test_place_order_refuses_fill_above_max_entry_price(db):
    """
    Regression test for the 2026-08-11 live losses: two directional entries
    were decided at best_ask 0.20/0.49 (under the 0.70 cap) but filled at
    0.99 (slippage 407%/104%) after the book moved during the fill latency,
    and settled at 0 — −$130 of losses from a price the decision-time gate
    never saw. The signal engine caps the DECISION ask; the broker must also
    refuse the ACTUAL walked fill price when it exceeds the cap, before any
    balance is debited.
    """
    good = {"tok_yes": make_symmetric_book("tok_yes", best_ask=0.49),
            "tok_no": make_symmetric_book("tok_no", best_ask=0.51)}
    # Book moves before the fill lands: the ask jumps from 0.49 to 0.99.
    bad = {"tok_yes": make_symmetric_book("tok_yes", best_ask=0.99),
           "tok_no": make_symmetric_book("tok_no", best_ask=0.51)}
    feed = MovingFeed(good, bad, good_for_first_n=1)  # decision=good, fill=bad
    market = make_market()
    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0,
                         simulated_fill_latency_s=0.3)

    with pytest.raises(EntryPriceExceededError):
        await broker.place_order(market, "YES", 100, max_entry_price=0.70)

    # Nothing opened, nothing debited.
    assert not broker.has_open_position("m1")
    assert broker.balance_usd == pytest.approx(1000)
    assert await db.get_open_trades(mode="PAPER") == []


async def test_place_order_accepts_fill_at_or_below_max_entry_price(db):
    """The cap must NOT block legitimate fills: a fill that stays under the
    cap goes through exactly as before."""
    books = {"tok_yes": make_symmetric_book("tok_yes", best_ask=0.55),
             "tok_no": make_symmetric_book("tok_no", best_ask=0.55)}
    market = make_market()
    broker = PaperBroker(db=db, feed=FakeFeed(books), starting_balance_usd=1000, fee_pct=0.0,
                         simulated_fill_latency_s=0.3)

    fill = await broker.place_order(market, "YES", 100, max_entry_price=0.70)
    assert fill.avg_price <= 0.70
    assert broker.has_open_position("m1")


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


async def test_sum_to_one_prechecks_total_cost_no_half_open_hedge(db):
    """The combo's TOTAL cost (both legs + fees) is validated before either
    leg opens. The sequential code could raise InsufficientBalanceError on the
    second leg after the first already opened — leaving a HALF-OPEN hedge with
    no reversal path (reviewed 2026-08-07)."""
    books = {"tok_yes": make_symmetric_book("tok_yes", best_ask=0.46),
             "tok_no": make_symmetric_book("tok_no", best_ask=0.48)}
    feed = FakeFeed(books)
    market = make_market()
    # Total cost = $100 + price-dependent fees (0.07 rate): at the 0.46/0.48
    # asks that is ~$1.74 -> $101.74 total; balance $101 covers one leg's
    # $51 but not both — each leg alone would pass its own per-leg check.
    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=101, fee_pct=0.07)

    opp = find_sum_to_one_opportunity(
        market, books["tok_yes"], books["tok_no"], min_edge_pct=0.01, fee_pct=0.07,
    )
    assert opp is not None

    with pytest.raises(InsufficientBalanceError):
        await broker.place_sum_to_one_order(opp, total_size_usd=100)

    # Nothing opened, nothing debited — no half-open hedge to clean up.
    assert await db.get_open_trades(mode="PAPER") == []
    assert broker.balance_usd == pytest.approx(101)


async def test_sum_to_one_refuses_when_real_walk_exceeds_1(db):
    """
    The pre-place quote guard: best asks can sum below $1 while the real
    ask-WALK (what the fills would ACTUALLY pay on a thin book) sums above
    it. The combo must be refused BEFORE any leg opens — never place a
    guaranteed-losing pair (live 2026-08-09: fills at 0.86+0.15=1.01 — a
    guaranteed loss that the reversal then amplified to −$46.72).
    """
    yes_book = OrderBook(
        market_id="m1", token_id="tok_yes",
        bids=(OrderBookLevel(price=0.40, size=1000),),
        asks=(OrderBookLevel(price=0.46, size=10), OrderBookLevel(price=0.90, size=1000)),
    )
    no_book = OrderBook(
        market_id="m1", token_id="tok_no",
        bids=(OrderBookLevel(price=0.40, size=1000),),
        asks=(OrderBookLevel(price=0.48, size=10), OrderBookLevel(price=0.50, size=1000)),
    )
    books = {"tok_yes": yes_book, "tok_no": no_book}
    feed = FakeFeed(books)
    market = make_market()
    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0)

    # Detected against BEST asks (0.46 + 0.48 = 0.94 < $1)...
    opp = find_sum_to_one_opportunity(market, yes_book, no_book, min_edge_pct=0.01, fee_pct=0.0)
    assert opp is not None

    # ...but the real walk (10 shares of depth at the best ask, the rest at
    # 0.90/0.50) costs ~1.33 — refused before opening anything.
    with pytest.raises(SumToOneEdgeLostError):
        await broker.place_sum_to_one_order(opp, total_size_usd=100)

    assert await db.get_open_trades(mode="PAPER") == []
    assert broker.balance_usd == pytest.approx(1000)  # nothing debited


async def test_sum_to_one_reverses_when_edge_evaporates_at_fill(db):
    """
    Regression test for the 2026-08-07 loss: the quote/decision see a < $1
    combo, but the fills land after the simulated fill latency against a
    book that kept moving — the combined fill cost crosses $1 (verified
    live: 0.31+0.73=1.04, 0.17+0.89=1.06). When REVERSING loses LESS than
    holding to settlement (bids stayed near the asks), both legs are
    reversed immediately and SumToOneEdgeLostError raised — never held as a
    large guaranteed loss.
    """
    good = {"tok_yes": make_symmetric_book("tok_yes", best_ask=0.46),
            "tok_no": make_symmetric_book("tok_no", best_ask=0.48)}
    # Book moves before the fills land: asks cross $1 (0.54 + 0.52 = 1.06)
    # while bids stay near them, so a reversal is cheaper than holding.
    bad = {"tok_yes": make_symmetric_book("tok_yes", best_bid=0.52, best_ask=0.54),
           "tok_no": make_symmetric_book("tok_no", best_bid=0.50, best_ask=0.52)}
    feed = MovingFeed(good, bad, good_for_first_n=2)
    market = make_market()
    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0,
                         simulated_fill_latency_s=0.3)

    opp = find_sum_to_one_opportunity(
        market, good["tok_yes"], good["tok_no"], min_edge_pct=0.01, fee_pct=0.0,
    )
    assert opp is not None  # detected as a genuine < $1 opportunity

    with pytest.raises(SumToOneEdgeLostError):
        await broker.place_sum_to_one_order(opp, total_size_usd=100)

    # Reversal was the cheaper option — both legs reversed, none remain.
    assert not broker.has_open_position("m1")
    open_trades = await db.get_open_trades(mode="PAPER")
    assert open_trades == []


async def test_sum_to_one_holds_when_reversal_loses_more_than_holding(db):
    """
    When the fills cross $1 but the current bids have collapsed (thin
    book), REVERSING loses more than holding to settlement — the pair must
    be HELD (both legs stay open, the combo resolves normally) rather than
    reversed into a deeper loss (live 2026-08-09: fills at 1.01, reversal
    sold at 0.865 → −$46.72).
    """
    good = {"tok_yes": make_symmetric_book("tok_yes", best_ask=0.46),
            "tok_no": make_symmetric_book("tok_no", best_ask=0.48)}
    # Asks moved up (fills cross $1) AND bids collapsed — reversing is far
    # worse than the worst-case hold.
    bad = {"tok_yes": make_symmetric_book("tok_yes", best_bid=0.40, best_ask=0.52),
           "tok_no": make_symmetric_book("tok_no", best_bid=0.40, best_ask=0.50)}
    feed = MovingFeed(good, bad, good_for_first_n=2)
    market = make_market()
    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0,
                         simulated_fill_latency_s=0.3)

    opp = find_sum_to_one_opportunity(
        market, good["tok_yes"], good["tok_no"], min_edge_pct=0.01, fee_pct=0.0,
    )
    assert opp is not None

    with pytest.raises(SumToOneEdgeLostError):
        await broker.place_sum_to_one_order(opp, total_size_usd=100)

    # Held to settlement: both legs still open, same combo_group_id.
    open_trades = await db.get_open_trades(mode="PAPER")
    assert len(open_trades) == 2
    assert len({t["combo_group_id"] for t in open_trades}) == 1
    assert broker.has_open_position("m1")


async def test_sum_to_one_held_when_edge_survives_fill(db):
    """When the fills land at or below the decision-time cost, the combo is
    held as a genuine arbitrage (both legs open, same combo_group_id)."""
    books = {"tok_yes": make_symmetric_book("tok_yes", best_ask=0.46),
             "tok_no": make_symmetric_book("tok_no", best_ask=0.48)}
    market = make_market()
    broker = PaperBroker(db=db, feed=FakeFeed(books), starting_balance_usd=1000, fee_pct=0.0,
                         simulated_fill_latency_s=0.3)

    opp = find_sum_to_one_opportunity(market, books["tok_yes"], books["tok_no"], min_edge_pct=0.01, fee_pct=0.0)
    yes_fill, no_fill = await broker.place_sum_to_one_order(opp, total_size_usd=100)

    assert yes_fill.avg_price + no_fill.avg_price < 1.0
    open_trades = await db.get_open_trades(mode="PAPER")
    assert len(open_trades) == 2
    assert len({t["combo_group_id"] for t in open_trades}) == 1  # same combo


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


async def test_close_position_early_refuses_partial_fill_on_thin_book(db):
    """
    FOK semantics: if the bid side cannot absorb the ENTIRE position, the
    exit must be refused rather than closing the trade at a phantom partial
    price. The old code booked a loss equal to the unfilled remainder and
    deleted the position's unrealized value.
    """
    books = {"tok_yes": make_symmetric_book("tok_yes"), "tok_no": make_symmetric_book("tok_no")}
    feed = FakeFeed(books)
    broker = PaperBroker(db=db, feed=feed, starting_balance_usd=1000, fee_pct=0.0)
    fill = await broker.place_order(make_market(), "YES", 100)  # ~178.6 shares @ 0.56

    # Shrink the bid side so it can only absorb ~80 of ~178.6 shares.
    books["tok_yes"] = OrderBook(
        market_id="m1", token_id="tok_yes",
        bids=(OrderBookLevel(price=0.60, size=80),),
        asks=(OrderBookLevel(price=0.62, size=1000),),
    )

    pnl = await broker.close_position_early(make_market(), fill.trade_id, reason="TAKE_PROFIT")
    assert pnl is None  # refused — cannot sell the whole position
    assert broker.has_open_position("m1")  # position survives, not deleted


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
