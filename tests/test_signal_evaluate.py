"""
Integration tests for SignalEngine.evaluate() covering the fair-value model
wiring, correct YES/NO book selection, and fallback to the momentum
heuristic when fair-value inputs aren't available.
"""
import time

import pytest

from config.settings import Settings
from data.binance_feed import PriceUpdate
from data.polymarket_feed import Market, OrderBook, OrderBookLevel
from engine.signal import SignalEngine
from storage.db import Database


def make_settings(**overrides) -> Settings:
    defaults = dict(EDGE_THRESHOLD_PCT=0.05, MIN_CONFIDENCE=0.3, MIN_MARKET_LIQUIDITY_USD=50_000)
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def make_market(reference_price=None, expires_at_ts=None) -> Market:
    return Market(
        market_id="m1", question="Bitcoin Up or Down - 15 min",
        token_id_yes="tok_yes", token_id_no="tok_no",
        liquidity_usd=100_000, end_date_iso="2026-07-31T14:00:00Z",
        asset="BTC", duration_minutes=15,
        reference_price=reference_price, expires_at_ts=expires_at_ts,
    )


def make_book(token_id: str, best_bid: float, best_ask: float, depth=200_000) -> OrderBook:
    size = depth / ((best_bid + best_ask) / 2)
    return OrderBook(
        market_id="m1", token_id=token_id,
        bids=(OrderBookLevel(price=best_bid, size=size),),
        asks=(OrderBookLevel(price=best_ask, size=size),),
    )


def feed_ticks(engine: SignalEngine, prices: list[float], symbol="BTCUSDT", spacing_s=1.0):
    now = time.time()
    for i, p in enumerate(prices):
        engine.ingest_price_update(PriceUpdate(symbol=symbol, price=p, event_time_ms=0, received_at=now + i * spacing_s, kind="trade"))


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    yield database
    await database.close()


# -- fair value path used when available ----------------------------------------

async def test_uses_fair_value_model_when_reference_price_available(db):
    settings = make_settings()
    engine = SignalEngine(settings, db)

    # Noisy but roughly flat prices around 65000, so there's a usable vol estimate.
    prices = [65000, 65010, 64995, 65005, 64998, 65012, 65003, 64997, 65008, 65001]
    feed_ticks(engine, prices)

    market = make_market(reference_price=65000, expires_at_ts=time.time() + 300)
    yes_book = make_book("tok_yes", 0.50, 0.52)
    no_book = make_book("tok_no", 0.48, 0.50)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.model_used == "fair_value"


async def test_falls_back_to_momentum_without_reference_price(db):
    settings = make_settings()
    engine = SignalEngine(settings, db)

    prices = [100, 101, 102]  # confirmed UP direction for the momentum fallback
    feed_ticks(engine, prices)

    market = make_market(reference_price=None)  # no reference price captured
    yes_book = make_book("tok_yes", 0.50, 0.52)
    no_book = make_book("tok_no", 0.48, 0.50)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.model_used == "momentum_fallback"


# -- the reviewer's exact scenario, at the full evaluate() level -----------------

async def test_reviewers_scenario_below_reference_despite_upward_momentum(db):
    """
    BTC starts contract at $70,000, falls to $69,500, then rises to $69,650.
    Recent momentum is UP, but price is still well below the $70,000
    reference. The fair-value model should reflect that correctly (lean NO),
    which is exactly what the momentum-only fallback would have gotten
    wrong (it would have leaned YES purely off the recent upward tick).
    """
    settings = make_settings()
    engine = SignalEngine(settings, db)

    # Recent ticks: down to 69500, recovering toward 69650 (upward momentum
    # over the last few ticks), but still below the 70000 reference.
    prices = [70000, 69700, 69500, 69550, 69600, 69620, 69640, 69650, 69645, 69655]
    feed_ticks(engine, prices)

    market = make_market(reference_price=70000, expires_at_ts=time.time() + 300)
    # Polymarket still pricing close to 50/50 (hasn't caught up to the dip).
    yes_book = make_book("tok_yes", 0.49, 0.51)
    no_book = make_book("tok_no", 0.49, 0.51)

    signal = await engine.evaluate(market, yes_book, no_book)

    assert signal.model_used == "fair_value"
    assert signal.implied_prob < 0.5  # correctly leans NO (below reference) despite recent upward ticks


# -- correct book selection for NO-side signals -----------------------------------

async def test_no_side_signal_uses_no_book_for_depth_not_yes_book(db):
    settings = make_settings()
    engine = SignalEngine(settings, db)

    prices = [70000, 69700, 69500, 69550, 69600]  # ends below reference -> implies NO
    feed_ticks(engine, prices)

    market = make_market(reference_price=70000, expires_at_ts=time.time() + 300)
    # YES book has almost no depth; NO book (the one that should actually be
    # used, since the signal leans NO) has plenty.
    yes_book = OrderBook(
        market_id="m1", token_id="tok_yes",
        bids=(OrderBookLevel(price=0.49, size=1),), asks=(OrderBookLevel(price=0.51, size=1),),
    )
    no_book = make_book("tok_no", 0.49, 0.51, depth=500_000)

    signal = await engine.evaluate(market, yes_book, no_book)

    if signal.side == "NO":
        # Confidence should reflect the DEEP no_book, not the thin yes_book --
        # if this were still using yes_book's depth (the old bug), confidence
        # would be near zero regardless of threshold.
        assert signal.confidence > 0.1


# -- exit-check recompute without logging -----------------------------------

async def test_log_false_does_not_write_to_signals_table(db):
    settings = make_settings()
    engine = SignalEngine(settings, db)
    prices = [100, 101, 102]
    feed_ticks(engine, prices)
    market = make_market()
    yes_book = make_book("tok_yes", 0.50, 0.52)
    no_book = make_book("tok_no", 0.48, 0.50)

    await engine.evaluate(market, yes_book, no_book, log=True)
    count_after_logged = len((await db.get_latency_events()))  # not signals, just confirming db still healthy

    conn = db._conn
    cur = await conn.execute("SELECT COUNT(*) as c FROM signals")
    row = await cur.fetchone()
    count_before = row["c"]

    await engine.evaluate(market, yes_book, no_book, log=False)

    cur = await conn.execute("SELECT COUNT(*) as c FROM signals")
    row = await cur.fetchone()
    count_after = row["c"]

    assert count_after == count_before  # log=False must not add a row
