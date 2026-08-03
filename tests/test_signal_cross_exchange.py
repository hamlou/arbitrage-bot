"""
Tests for the cross-exchange sanity gate in SignalEngine.evaluate(): the
engine must refuse to fire a signal when the latest known Binance and
Coinbase prices for an asset disagree by more than
CROSS_EXCHANGE_TOLERANCE_PCT, logging the skip at INFO with reason
cross_exchange_disagreement (never an error) — and must remain fail-open
when Coinbase hasn't delivered a tick yet. No network anywhere.
"""
import logging
import time

import pytest

from config.settings import Settings
from data.binance_feed import PriceUpdate
from data.coinbase_feed import PriceUpdate as CoinbasePriceUpdate
from data.polymarket_feed import Market, OrderBook, OrderBookLevel
from engine.signal import SignalEngine
from storage.db import Database


def make_settings(**overrides) -> Settings:
    defaults = dict(EDGE_THRESHOLD_PCT=0.05, MIN_CONFIDENCE=0.3, MIN_MARKET_LIQUIDITY_USD=50_000)
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def make_market(reference_price=None) -> Market:
    return Market(
        market_id="m1", question="Bitcoin Up or Down - 15 min",
        token_id_yes="tok_yes", token_id_no="tok_no",
        liquidity_usd=100_000, end_date_iso="2026-07-31T14:00:00Z",
        asset="BTC", duration_minutes=15,
        reference_price=reference_price, expires_at_ts=time.time() + 300,
    )


def make_book(token_id: str, best_bid: float, best_ask: float, depth=200_000) -> OrderBook:
    size = depth / ((best_bid + best_ask) / 2)
    return OrderBook(
        market_id="m1", token_id=token_id,
        bids=(OrderBookLevel(price=best_bid, size=size),),
        asks=(OrderBookLevel(price=best_ask, size=size),),
    )


def feed_binance(engine: SignalEngine, prices: list[float], symbol="BTCUSDT"):
    now = time.time()
    for i, p in enumerate(prices):
        engine.ingest_price_update(
            PriceUpdate(symbol=symbol, price=p, event_time_ms=0, received_at=now + i, kind="trade"),
            source="binance",
        )


def feed_coinbase(engine: SignalEngine, price: float, symbol="BTCUSDT", received_at=None):
    engine.ingest_price_update(
        CoinbasePriceUpdate(symbol=symbol, price=price, event_time_ms=0,
                            received_at=received_at or time.time(), kind="trade"),
        source="coinbase",
    )


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    yield database
    await database.close()


# -- the gate itself -----------------------------------------------------------


async def test_fires_when_prices_agree(db):
    settings = make_settings()
    engine = SignalEngine(settings, db)

    prices = [100, 101, 102]  # momentum fallback: clear UP direction
    feed_binance(engine, prices)
    feed_coinbase(engine, 102.01)  # 0.01% off Binance's 102 — well inside 0.1% tolerance

    market = make_market(reference_price=100)
    # Polymarket pricing the YES side far below the model's lean -> clear edge.
    yes_book = make_book("tok_yes", 0.38, 0.40)
    no_book = make_book("tok_no", 0.60, 0.62)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.fired is True


async def test_blocks_when_prices_disagree(db):
    settings = make_settings()
    engine = SignalEngine(settings, db)

    prices = [100, 101, 102]
    feed_binance(engine, prices)
    feed_coinbase(engine, 110.0)  # ~7.8% off Binance's 102 — far beyond 0.1%

    market = make_market(reference_price=100)
    yes_book = make_book("tok_yes", 0.38, 0.40)
    no_book = make_book("tok_no", 0.60, 0.62)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.fired is False
    assert signal.reason == "cross_exchange_disagreement"


async def test_disagreement_logged_at_info_not_error(db, caplog):
    settings = make_settings()
    engine = SignalEngine(settings, db)
    feed_binance(engine, [100, 101, 102])
    feed_coinbase(engine, 110.0)

    market = make_market(reference_price=100)
    yes_book = make_book("tok_yes", 0.38, 0.40)
    no_book = make_book("tok_no", 0.60, 0.62)

    with caplog.at_level(logging.INFO, logger="engine.signal"):
        signal = await engine.evaluate(market, yes_book, no_book)

    assert signal.reason == "cross_exchange_disagreement"
    info_lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("cross_exchange_disagreement" in line for line in info_lines)
    # Must never be logged as an error.
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_gate_fail_open_when_no_coinbase_tick_yet(db):
    """With only Binance data (e.g. Coinbase feed down), the gate cannot judge
    and must NOT block — the bot keeps working on Binance alone."""
    settings = make_settings()
    engine = SignalEngine(settings, db)
    feed_binance(engine, [100, 101, 102])
    # no feed_coinbase call

    market = make_market(reference_price=100)
    yes_book = make_book("tok_yes", 0.38, 0.40)
    no_book = make_book("tok_no", 0.60, 0.62)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.fired is True
    assert signal.reason == "OK"


async def test_blocks_at_any_edge_strength_when_prices_disagree(db):
    """Even a huge model edge must not fire while the exchanges disagree —
    the gate is checked before edge/confidence are even consulted."""
    settings = make_settings(EDGE_THRESHOLD_PCT=0.001, MIN_CONFIDENCE=0.01)
    engine = SignalEngine(settings, db)
    feed_binance(engine, [100, 101, 102])
    feed_coinbase(engine, 200.0)  # ~96% off

    market = make_market(reference_price=100)
    yes_book = make_book("tok_yes", 0.10, 0.12)  # massive apparent edge
    no_book = make_book("tok_no", 0.88, 0.90)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.fired is False
    assert signal.reason == "cross_exchange_disagreement"
    # The model read is untrustworthy while exchanges disagree — side and edge
    # are zeroed so downstream consumers (e.g. early-exit checks, which don't
    # test `fired`) can't act on a gated reading.
    assert signal.side == ""
    assert signal.edge_pct == 0.0


async def test_stale_coinbase_tick_treated_as_missing_not_disagreement(db):
    """A silently-stalled Coinbase feed (connection alive, but no fresh
    trades) must NOT block trading — a stale tick is "can't judge", not a
    real disagreement. Otherwise a stall becomes an accidental kill switch."""
    settings = make_settings()
    engine = SignalEngine(settings, db)
    feed_binance(engine, [100, 101, 102])
    # Coinbase last spoke 10 minutes ago; price is 110 (hugely off 102) — but
    # it's stale, so the gate must treat it as missing and allow the signal.
    feed_coinbase(engine, 110.0, received_at=time.time() - 600)

    market = make_market(reference_price=100)
    yes_book = make_book("tok_yes", 0.38, 0.40)
    no_book = make_book("tok_no", 0.60, 0.62)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.fired is True
    assert signal.reason == "OK"


async def test_stale_binance_tick_also_fail_open(db):
    """Same staleness rule applies to the Binance side of the comparison."""
    settings = make_settings()
    engine = SignalEngine(settings, db)
    now = time.time()
    # Fresh-ish binance ticks for the model tracker...
    for i, p in enumerate([100, 101, 102]):
        engine.ingest_price_update(
            PriceUpdate(symbol="BTCUSDT", price=p, event_time_ms=0, received_at=now + i, kind="trade"),
            source="binance",
        )
    # ...but the engine's own latest-price map already holds an ancient value.
    engine._latest_received_at[("binance", "BTCUSDT")] = now - 600
    feed_coinbase(engine, 110.0)  # fresh, but binance side is stale

    market = make_market(reference_price=100)
    yes_book = make_book("tok_yes", 0.38, 0.40)
    no_book = make_book("tok_no", 0.60, 0.62)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.fired is True  # fail-open: can't judge on stale data


async def test_inactive_gate_logged_at_debug_not_error(db, caplog):
    """When the gate can't be evaluated (no Coinbase data), that's visible at
    DEBUG so operators know the sanity check is off — never an error."""
    settings = make_settings()
    engine = SignalEngine(settings, db)
    feed_binance(engine, [100, 101, 102])

    market = make_market(reference_price=100)
    yes_book = make_book("tok_yes", 0.38, 0.40)
    no_book = make_book("tok_no", 0.60, 0.62)

    with caplog.at_level(logging.DEBUG, logger="engine.signal"):
        signal = await engine.evaluate(market, yes_book, no_book)

    assert signal.fired is True
    assert any("gate inactive" in r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


# -- source isolation ----------------------------------------------------------


def test_coinbase_ticks_never_feed_the_model_tracker():
    engine = SignalEngine(make_settings(), Database(":memory:"))
    feed_coinbase(engine, 100.0)

    # The model tracker for BTCUSDT must stay empty — Coinbase is gate-only.
    assert "BTCUSDT" not in engine._trackers
    assert engine.current_price("BTC") is None


def test_cross_exchange_disagreement_pct_helper():
    engine = SignalEngine(make_settings(), Database(":memory:"))
    assert engine.cross_exchange_disagreement_pct("BTCUSDT") is None  # no ticks yet

    feed_binance(engine, [100.0])
    assert engine.cross_exchange_disagreement_pct("BTCUSDT") is None  # no coinbase yet

    feed_coinbase(engine, 101.0)
    assert engine.cross_exchange_disagreement_pct("BTCUSDT") == 1.0  # 1%


# -- DB audit rows (exchange_disagreements) ------------------------------------


async def test_disagreement_writes_db_row_even_when_no_signal_would_fire(db):
    """The audit row must exist for EVERY above-threshold disagreement, not
    just ones that suppressed a would-be signal — so disagreement frequency
    can be reviewed later. Here the edge/confidence thresholds are set so
    high no signal would ever fire anyway, and a row must still be written."""
    settings = make_settings(EDGE_THRESHOLD_PCT=0.99, MIN_CONFIDENCE=0.99)
    engine = SignalEngine(settings, db)
    feed_binance(engine, [100, 101, 102])
    feed_coinbase(engine, 110.0)  # ~7.8% off — far beyond 0.1%

    market = make_market(reference_price=100)
    yes_book = make_book("tok_yes", 0.38, 0.40)
    no_book = make_book("tok_no", 0.60, 0.62)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.fired is False
    assert signal.reason == "cross_exchange_disagreement"

    rows = await db.get_exchange_disagreements(symbol="BTCUSDT")
    assert len(rows) == 1
    assert rows[0]["binance_price"] == 102.0
    assert rows[0]["coinbase_price"] == 110.0
    assert rows[0]["disagreement_pct"] == pytest.approx(abs(110.0 - 102.0) / 102.0 * 100.0)
    assert rows[0]["symbol"] == "BTCUSDT"


async def test_disagreement_writes_db_row_during_insufficient_data(db, caplog):
    """The disagreement is a pure price comparison, independent of model
    readiness — so the audit row must be written even when evaluate() early-
    returns on insufficient data (no fair-value inputs, no confirmed
    momentum). This was the reviewer's key gap: disagreements during such
    windows were never logged before."""
    settings = make_settings()
    engine = SignalEngine(settings, db)
    # Only ONE binance tick (no reference price on the market either) — the
    # model has nothing to work with and evaluate() will early-return on
    # insufficient data. The prices themselves still disagree strongly.
    now = time.time()
    engine.ingest_price_update(
        PriceUpdate(symbol="BTCUSDT", price=100.0, event_time_ms=0, received_at=now, kind="trade"),
        source="binance",
    )
    feed_coinbase(engine, 110.0)

    market = make_market(reference_price=None)  # no reference -> fair value impossible
    yes_book = make_book("tok_yes", 0.38, 0.40)
    no_book = make_book("tok_no", 0.60, 0.62)

    with caplog.at_level(logging.INFO, logger="engine.signal"):
        signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.fired is False
    assert "insufficient data" in signal.reason
    # The disagreement was still logged at INFO even though no signal could
    # have fired — not silently dropped on the early-return path.
    info_lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("cross_exchange_disagreement" in line for line in info_lines)

    rows = await db.get_exchange_disagreements(symbol="BTCUSDT")
    assert len(rows) == 1  # the disagreement was still recorded
    assert rows[0]["disagreement_pct"] == pytest.approx(10.0)  # |110-100|/100


async def test_disagreement_writes_db_row_on_log_false_recompute(db):
    """log=False recomputes (the ONLY evaluate calls markets with an open
    position get, from _check_early_exits) must also record the audit row —
    gating on `log` would silently drop every held-market observation."""
    settings = make_settings()
    engine = SignalEngine(settings, db)
    feed_binance(engine, [100, 101, 102])
    feed_coinbase(engine, 110.0)

    market = make_market(reference_price=100)
    yes_book = make_book("tok_yes", 0.38, 0.40)
    no_book = make_book("tok_no", 0.60, 0.62)

    signal = await engine.evaluate(market, yes_book, no_book, log=False)
    assert signal.fired is False
    assert signal.reason == "cross_exchange_disagreement"
    # Blocked state still zeroes side/edge even on the exit-check path, so
    # EDGE_REVERSAL can't fire off a gated reading.
    assert signal.side == ""
    assert signal.edge_pct == 0.0

    rows = await db.get_exchange_disagreements(symbol="BTCUSDT")
    assert len(rows) == 1
    # log=False must NOT add a signals-table row — only the disagreement
    # audit row (the exit-check recompute shouldn't pollute the signal trail).
    conn = db._conn
    cur = await conn.execute("SELECT COUNT(*) as c FROM signals")
    (signal_count,) = await cur.fetchone()
    assert signal_count == 0


async def test_disagreement_no_db_row_when_below_threshold(db):
    """Rows are written only when the difference EXCEEDS the threshold — a
    small inside-tolerance basis is not recorded."""
    settings = make_settings()
    engine = SignalEngine(settings, db)
    feed_binance(engine, [100, 101, 102])
    feed_coinbase(engine, 102.01)  # 0.01% off 102 — inside 0.1% tolerance

    market = make_market(reference_price=100)
    yes_book = make_book("tok_yes", 0.38, 0.40)
    no_book = make_book("tok_no", 0.60, 0.62)

    signal = await engine.evaluate(market, yes_book, no_book, log=False)
    assert signal.fired is True  # allowed through, gate inactive on disagreement

    rows = await db.get_exchange_disagreements()
    assert rows == []



async def test_no_db_row_when_prices_agree(db):
    settings = make_settings()
    engine = SignalEngine(settings, db)
    feed_binance(engine, [100, 101, 102])
    feed_coinbase(engine, 102.01)  # 0.01% off — inside tolerance

    market = make_market(reference_price=100)
    yes_book = make_book("tok_yes", 0.38, 0.40)
    no_book = make_book("tok_no", 0.60, 0.62)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.fired is True

    rows = await db.get_exchange_disagreements()
    assert rows == []


async def test_no_db_row_when_gate_inactive(db):
    """No Coinbase data -> gate can't judge -> no disagreement is recorded."""
    settings = make_settings()
    engine = SignalEngine(settings, db)
    feed_binance(engine, [100, 101, 102])

    market = make_market(reference_price=100)
    yes_book = make_book("tok_yes", 0.38, 0.40)
    no_book = make_book("tok_no", 0.60, 0.62)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.fired is True

    rows = await db.get_exchange_disagreements()
    assert rows == []


async def test_db_log_exchange_disagreement_roundtrip(db):
    """The DB-layer method itself: insert then read back, newest first."""
    await db.log_exchange_disagreement(
        symbol="BTCUSDT", binance_price=100.0, coinbase_price=101.0, disagreement_pct=1.0,
    )
    await db.log_exchange_disagreement(
        symbol="ETHUSDT", binance_price=2000.0, coinbase_price=2010.0, disagreement_pct=0.5,
    )

    all_rows = await db.get_exchange_disagreements()
    assert len(all_rows) == 2
    assert all_rows[0]["symbol"] == "ETHUSDT"  # newest first
    assert all_rows[0]["disagreement_pct"] == 0.5
    assert all_rows[1]["symbol"] == "BTCUSDT"
    assert all_rows[1]["binance_price"] == 100.0
    assert all_rows[1]["coinbase_price"] == 101.0

    eth_rows = await db.get_exchange_disagreements(symbol="ETHUSDT")
    assert len(eth_rows) == 1
    assert eth_rows[0]["symbol"] == "ETHUSDT"


# -- settings ------------------------------------------------------------------


def test_cross_exchange_tolerance_default():
    settings = Settings(_env_file=None)
    assert settings.CROSS_EXCHANGE_TOLERANCE_PCT == 0.1
