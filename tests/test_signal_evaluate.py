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
    # The fallback path still exists for explicit opt-in (backtest/replay);
    # this test enables it to keep the machinery itself covered.
    settings = make_settings(ALLOW_MOMENTUM_FALLBACK_ENTRIES=True)
    engine = SignalEngine(settings, db)

    prices = [100, 101, 102]  # confirmed UP direction for the momentum fallback
    feed_ticks(engine, prices)

    market = make_market(reference_price=None)  # no reference price captured
    yes_book = make_book("tok_yes", 0.50, 0.52)
    no_book = make_book("tok_no", 0.48, 0.50)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.model_used == "momentum_fallback"


# -- the reviewer's exact scenario, at the full evaluate() level -----------------

async def test_fallback_fires_on_net_momentum_with_oscillating_ticks(db):
    """
    Regression guard for the 2026-08-08 live bug: the momentum fallback
    required CONFIRMATION_WINDOW consecutive same-direction ticks, which
    trade-by-trade BTC data almost never produces (66% of 16,019 live signals
    died with "insufficient data", 0 directional trades in 20.7h). Oscillating
    ticks with a NET upward drift must now reach the fallback via the sign of
    the net window momentum. Explicitly enabled here to cover the machinery.
    """
    settings = make_settings(EDGE_THRESHOLD_PCT=0.01, MIN_CONFIDENCE=0.2, ALLOW_MOMENTUM_FALLBACK_ENTRIES=True)
    engine = SignalEngine(settings, db)

    # Up/down/up/down — direction_confirmed() is None (no 3 consecutive same-
    # direction ticks) — but the net 30s move is clearly positive.
    prices = [100.00, 100.08, 100.04, 100.12, 100.06, 100.16, 100.10, 100.20, 100.14, 100.24]
    feed_ticks(engine, prices)

    market = make_market(reference_price=None)  # no reference -> fair value impossible
    yes_book = make_book("tok_yes", 0.45, 0.47)
    no_book = make_book("tok_no", 0.51, 0.53)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.model_used == "momentum_fallback"  # the net-momentum sign path, not insufficient data
    assert signal.implied_prob > 0.5  # leans YES on net up-drift


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


# -- entry-price discipline guardrails (2026-08-06 fixes) ---------------------
# NOTE: these use the FAIR-VALUE path (reference price far from spot so the
# z-score saturates) because the momentum fallback's implied prob stays close
# to 0.5 for modest moves and would target the cheap side, not the rich one.


def feed_fair_value_ticks(engine: SignalEngine) -> None:
    """Noisy ticks around ~66000 with reference far below (60000) so the
    fair-value z-score saturates toward 1.0 and the model targets YES."""
    prices = [66000, 66010, 65995, 66005, 66000, 66015, 65998, 66008, 66002, 66012]
    feed_ticks(engine, prices)


async def test_blocks_entry_when_ask_above_max_directional_entry_price(db):
    """
    Regression guard: the bot bought YES @ 0.82 and NO @ 0.99 on overconfident
    model reads and lost ~$170 on two trades. Buying a token above
    MAX_DIRECTIONAL_ENTRY_PRICE means break-even requires being right 80%+ of
    the time after fees — the signal must not fire.
    """
    settings = make_settings(
        EDGE_THRESHOLD_PCT=0.05, MIN_CONFIDENCE=0.3, MIN_MARKET_LIQUIDITY_USD=50_000,
        MAX_DIRECTIONAL_ENTRY_PRICE=0.80, TAKER_FEE_PCT=0.02,
    )
    engine = SignalEngine(settings, db)

    feed_fair_value_ticks(engine)
    market = make_market(reference_price=60000, expires_at_ts=time.time() + 300)
    # Model leans YES (implied ~0.98), but the YES ask at 0.85 is above the cap.
    yes_book = make_book("tok_yes", 0.83, 0.85)
    no_book = make_book("tok_no", 0.13, 0.15)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.side == "YES"
    assert signal.fired is False
    assert "max" in signal.reason  # blocked specifically by the price cap


async def test_allows_entry_when_ask_below_max_directional_entry_price(db):
    """A reasonable ask with a non-degenerate model read must still be allowed
    to fire — the cap and the saturation guard are guardrails, not a kill
    switch. Uses the momentum fallback (no reference price) so the read stays
    inside the sane band. Fallback explicitly enabled for this test."""
    settings = make_settings(
        EDGE_THRESHOLD_PCT=0.05, MIN_CONFIDENCE=0.3, MIN_MARKET_LIQUIDITY_USD=50_000,
        MAX_DIRECTIONAL_ENTRY_PRICE=0.80, TAKER_FEE_PCT=0.02,
        ALLOW_MOMENTUM_FALLBACK_ENTRIES=True,
    )
    engine = SignalEngine(settings, db)

    # Moderate confirmed upward momentum -> implied ~0.66, not saturated.
    feed_ticks(engine, [100, 101, 102])
    market = make_market(reference_price=None)
    yes_book = make_book("tok_yes", 0.48, 0.50)
    no_book = make_book("tok_no", 0.48, 0.50)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.model_used == "momentum_fallback"
    assert signal.fired is True


async def test_edge_gate_is_fee_aware(db):
    """The raw model-vs-market gap must clear the taker fee before it counts as
    an edge — otherwise the "edge" is entirely consumed by fees. Uses a
    non-degenerate momentum read with aligned direction so only the fee gate
    can be the blocker. Fallback explicitly enabled for this test."""
    settings = make_settings(
        EDGE_THRESHOLD_PCT=0.05, MIN_CONFIDENCE=0.3, MIN_MARKET_LIQUIDITY_USD=50_000,
        MAX_DIRECTIONAL_ENTRY_PRICE=0.95, TAKER_FEE_PCT=0.04,  # high fee on purpose
        ALLOW_MOMENTUM_FALLBACK_ENTRIES=True,
    )
    engine = SignalEngine(settings, db)

    # Downward move -> implied ~0.45, model targets NO (aligned with the
    # momentum so the fresh-move gate passes). Market mid 0.49 -> raw edge
    # ~0.04; minus 0.04 fee leaves ~0 net < 0.05 threshold -> must not fire.
    feed_ticks(engine, [100.6, 100.3, 100.0])
    market = make_market(reference_price=None)
    yes_book = make_book("tok_yes", 0.48, 0.50)
    no_book = make_book("tok_no", 0.48, 0.50)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.fired is False
    assert "net edge" in signal.reason


# -- fresh-move / time-remaining entry gates (2026-08-07 fixes) --------------
# The bot bought NO @ 0.69 (implied NO 0.86) while BTC was actually RISING and
# Polymarket held YES at 0.30-0.33 — a 25s drift from a stale reference price,
# not a lag. The position hit ~$0 in 5s (-$35). The strategy's premise is
# "Polymarket LAGS a fresh Binance move", so the model direction must agree
# with actual recent price momentum, and entries must not happen in the final
# stretch of a window.


async def test_blocks_when_momentum_opposes_model_direction(db):
    """The model leans YES (price far above a low reference) but the recent
    price is actually FALLING. There is no fresh move in the model's
    direction — the market is repricing against the model, so this is a
    disagreement, not a lag. Must not fire."""
    settings = make_settings(
        EDGE_THRESHOLD_PCT=0.05, MIN_CONFIDENCE=0.3, MIN_MARKET_LIQUIDITY_USD=50_000,
        MAX_DIRECTIONAL_ENTRY_PRICE=0.95, TAKER_FEE_PCT=0.02,
    )
    engine = SignalEngine(settings, db)

    # Falling hard: 66000 -> 64200 over 10s. Model (reference 60000) says YES
    # with huge confidence, but the price is moving DOWN against it.
    prices = [66000, 65800, 65600, 65400, 65200, 65000, 64800, 64600, 64400, 64200]
    feed_ticks(engine, prices)
    market = make_market(reference_price=60000, expires_at_ts=time.time() + 300)
    yes_book = make_book("tok_yes", 0.48, 0.50)
    no_book = make_book("tok_no", 0.48, 0.50)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.side == "YES"  # the model still leans YES
    assert signal.fired is False
    assert "no fresh aligned move" in signal.reason


async def test_large_edge_bypasses_fresh_move_magnitude_floor(db):
    """Verified 2026-08-09 on 41k logged signals: the fresh-move gate blocked
    4,033 fair-value signals with edge > 5pt, and 90% of those markets later
    converged toward the model's read (95% simulated win rate). When the edge
    is >= FRESH_MOVE_LARGE_EDGE_BYPASS_PCT, a tiny-but-aligned move must pass
    the gate — the divergence itself is the signal."""
    settings = make_settings(
        EDGE_THRESHOLD_PCT=0.05, MIN_CONFIDENCE=0.3, MIN_MARKET_LIQUIDITY_USD=50_000,
        MAX_DIRECTIONAL_ENTRY_PRICE=0.95, TAKER_FEE_PCT=0.02,
        FRESH_MOVE_LARGE_EDGE_BYPASS_PCT=0.12,
        ALLOW_MOMENTUM_FALLBACK_ENTRIES=True,
    )
    engine = SignalEngine(settings, db)

    # Big downward move (64000 -> 62000, ~3%), market priced at 0.55 — the
    # model leans NO hard (implied NO ~0.90+, edge >> 0.12). Then a tiny
    # continued drift DOWN (62000 -> 61980) so the 15s move is aligned in
    # direction but below FRESH_MOVE_MIN_PCT in magnitude. (Fewer than the
    # 8-tick volatility minimum, so this exercises the fallback path — hence
    # the explicit enable above.)
    feed_ticks(engine, [64000, 63000, 62000, 61990, 61980])
    market = make_market(reference_price=64000, expires_at_ts=time.time() + 300)
    yes_book = make_book("tok_yes", 0.54, 0.56)
    no_book = make_book("tok_no", 0.44, 0.46)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.side == "NO"  # model still leans NO
    assert signal.fired is True  # large edge bypasses the magnitude floor


async def test_large_edge_still_blocks_when_direction_opposes(db):
    """The bypass drops the MAGNITUDE floor only — the recent move must still
    agree in DIRECTION with the model. A huge edge with the market moving the
    OTHER way is a drift/repricing against the model, exactly what the gate
    exists to stop."""
    settings = make_settings(
        EDGE_THRESHOLD_PCT=0.05, MIN_CONFIDENCE=0.3, MIN_MARKET_LIQUIDITY_USD=50_000,
        MAX_DIRECTIONAL_ENTRY_PRICE=0.95, TAKER_FEE_PCT=0.02,
        FRESH_MOVE_LARGE_EDGE_BYPASS_PCT=0.12,
    )
    engine = SignalEngine(settings, db)

    # Price far BELOW reference (fair-value model says NO hard) while price is
    # RISING back toward it — opposite direction to the model's lean. Enough
    # ticks for the volatility estimator so fair-value engages (not fallback).
    feed_ticks(engine, [60000, 60050, 60100, 60200, 60400, 60800, 61400, 62200, 63200, 64400, 64500])
    market = make_market(reference_price=70000, expires_at_ts=time.time() + 300)
    yes_book = make_book("tok_yes", 0.50, 0.52)
    no_book = make_book("tok_no", 0.48, 0.50)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.side == "NO"  # model says NO (price below reference)
    assert signal.fired is False  # but price is RISING against the model
    assert "no fresh aligned move" in signal.reason


async def test_blocks_entry_when_window_almost_over(db):
    """Even with a genuine aligned move, entering in the final seconds of a
    window is a noise trade — the market has effectively decided. Must not
    fire. Fallback explicitly enabled so the time-remaining gate is what's
    tested, not the fallback gate."""
    settings = make_settings(
        EDGE_THRESHOLD_PCT=0.05, MIN_CONFIDENCE=0.3, MIN_MARKET_LIQUIDITY_USD=50_000,
        MAX_DIRECTIONAL_ENTRY_PRICE=0.95, TAKER_FEE_PCT=0.02,
        ALLOW_MOMENTUM_FALLBACK_ENTRIES=True,
    )
    engine = SignalEngine(settings, db)

    # Genuine upward momentum, market priced 0.48/0.50 — everything about the
    # signal is fine except there are only 30 seconds left.
    feed_ticks(engine, [100, 100.5, 101, 101.5, 102])
    market = make_market(reference_price=None, expires_at_ts=time.time() + 30)
    yes_book = make_book("tok_yes", 0.48, 0.50)
    no_book = make_book("tok_no", 0.48, 0.50)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.fired is False
    assert "left" in signal.reason and "minimum" in signal.reason


async def test_implied_probability_clamped_and_blocks_saturated_read(db):
    """The fair-value model saturates to ~0.0 / ~1.0 on 5-minute windows
    (verified live: implied=0.99999998 vs market 0.085). A degenerate read
    must be clamped AND must not fire even after clamping — otherwise a
    saturated read (e.g. the real YES @ 0.45, -$88.43 loss) still clears the
    edge threshold on its clamped value."""
    settings = make_settings(
        EDGE_THRESHOLD_PCT=0.05, MIN_CONFIDENCE=0.3, MIN_MARKET_LIQUIDITY_USD=50_000,
        MAX_DIRECTIONAL_ENTRY_PRICE=0.95, TAKER_FEE_PCT=0.02,
    )
    engine = SignalEngine(settings, db)

    feed_fair_value_ticks(engine)
    market = make_market(reference_price=60000, expires_at_ts=time.time() + 30)
    yes_book = make_book("tok_yes", 0.60, 0.62)
    no_book = make_book("tok_no", 0.36, 0.38)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert 0.02 <= signal.implied_prob <= 0.98  # clamped
    assert signal.fired is False  # saturated read is not tradable
    assert "saturated" in signal.reason


# -- exit-check recompute without logging -----------------------------------

# -- momentum fallback gate (2026-08-12) --------------------------------------
# Live 2026-08-11 data: all three full-stake SETTLED-at-zero losses came from
# momentum_fallback entries (no reference price = pure guess). By default the
# fallback must NOT fire entries — only the fair-value model (real reference +
# volatility) may trade.


async def test_momentum_fallback_gated_off_by_default(db):
    """No reference price -> fair value impossible. The momentum fallback
    would have produced a read, but with ALLOW_MOMENTUM_FALLBACK_ENTRIES=False
    (default) the signal must NOT fire and must explain why."""
    settings = make_settings()  # default: fallback gated off
    engine = SignalEngine(settings, db)

    prices = [100, 101, 102]  # confirmed UP direction
    feed_ticks(engine, prices)

    market = make_market(reference_price=None)  # no reference price captured
    yes_book = make_book("tok_yes", 0.50, 0.52)
    no_book = make_book("tok_no", 0.48, 0.50)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.fired is False
    assert signal.model_used == "momentum_fallback"  # audit trail still tags it
    assert "momentum fallback disabled" in signal.reason


async def test_momentum_fallback_gate_still_blocks_with_big_edge(db):
    """Even a large fallback edge must not fire while the gate is off — a
    big edge on a no-reference read is a bigger gamble, not a better one."""
    settings = make_settings(EDGE_THRESHOLD_PCT=0.01, MIN_CONFIDENCE=0.2)
    engine = SignalEngine(settings, db)

    # Strong net upward momentum -> fallback implied prob well above 0.5.
    prices = [100.0, 100.8, 100.4, 101.2, 100.8, 101.6, 101.2, 102.0]
    feed_ticks(engine, prices)

    market = make_market(reference_price=None)
    yes_book = make_book("tok_yes", 0.45, 0.47)  # market well below model -> large edge
    no_book = make_book("tok_no", 0.51, 0.53)

    signal = await engine.evaluate(market, yes_book, no_book)
    assert signal.fired is False
    assert "momentum fallback disabled" in signal.reason


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
