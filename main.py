"""
polymarket-arb-bot entry point.

Run with: python main.py

There is exactly one function in this file that decides paper vs. live —
get_broker() — and it is not something any other code path bypasses.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional, Union

from alerts.telegram import AlertLevel, build_alerter, build_reporter
from config.settings import settings
from data.binance_feed import BinanceFeed, PriceUpdate
from data.coinbase_feed import CoinbaseFeed
from data.polymarket_feed import Market, OrderBook, PolymarketFeed
from data.polymarket_ws_feed import PolymarketWSFeed
from engine.broker_live import LiveBroker, LiveTradingNotEnabledError, build_live_broker
from engine.broker_paper import PaperBroker, SumToOneEdgeLostError
from engine.calibration import load_calibration
from engine.feed_health import FeedHealth
from engine.lag_tracker import LagMeasurement, LagTracker
from engine.latency import LatencyTracker
from engine.risk import RiskManager, SignalForSizing
from engine.signal import SignalEngine
from engine.sum_to_one import find_sum_to_one_opportunity
from storage.db import Database
from ui.dashboard import DashboardState, run_dashboard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("main")

POLL_INTERVAL_S = 1.0
SETTLEMENT_POLL_INTERVAL_S = 5.0   # resolution doesn't need to be checked as often as signals are evaluated
MAX_CONCURRENT_MARKET_EVALS = 10   # cap concurrency to stay within CLOB rate limits


def fast_path_should_run(
    last_price: Optional[float],
    price: float,
    last_ts: Optional[float],
    received_at: float,
    trigger_pct: float,
    cooldown_s: float,
) -> bool:
    """
    Pure decision function for the event-driven fast path: should a Binance
    tick at `price` trigger an immediate evaluation?

      - No prior evaluation for this symbol -> yes (first tick arms the path).
      - Within the per-asset cooldown -> no (don't thrash on every micro-tick).
      - Cumulative move since the last evaluation below the trigger -> no.
      - Otherwise -> yes.

    Comparing against the LAST EVALUATED price (not the previous tick) makes
    the trigger cumulative, so a slow multi-tick drift that totals a
    meaningful move still fires.
    """
    if last_price is None or last_ts is None:
        return True
    if received_at - last_ts < cooldown_s:
        return False
    if last_price <= 0:
        return True
    return abs(price - last_price) / last_price >= trigger_pct


async def get_broker(db: Database, feed: PolymarketFeed, alerter) -> Union[PaperBroker, LiveBroker]:
    """
    The single decision point for paper vs. live. Returns PaperBroker unless
    ALL THREE LIVE_TRADING_CONFIRMED_* flags are True AND PAPER_MODE is
    explicitly False — in which case it returns a LiveBroker via the gated
    factory in engine/broker_live.py.

    There is no other code path in this project that trades live.
    """
    try:
        return await build_live_broker(settings, alerter)
    except LiveTradingNotEnabledError:
        logger.info("Live trading gate not satisfied (this is the default and expected state) — using PaperBroker")
        return PaperBroker(
            db=db,
            feed=feed,
            starting_balance_usd=settings.STARTING_PAPER_BALANCE_USD,
            fee_pct=settings.TAKER_FEE_PCT,  # single source of truth for the fee
            simulated_fill_latency_s=settings.SIMULATED_FILL_LATENCY_S,
            min_order_size_usd=settings.MIN_ORDER_SIZE_USD,
            tick_size=settings.TICK_SIZE,
        )


class TradingApp:
    def __init__(self):
        self.db = Database(settings.DATABASE_PATH)
        self.alerter = build_alerter(
            settings.TELEGRAM_BOT_TOKEN,
            settings.TELEGRAM_CHAT_ID,
            muted=settings.TELEGRAM_MUTED_DEFAULT,
        )
        # Two-way Telegram channel: pushes the periodic status digest AND answers
        # /status /stats /help via polling. status_provider feeds the digest
        # with a live snapshot — bound method, safe to pass before setup().
        self.telegram_reporter = build_reporter(
            settings.TELEGRAM_BOT_TOKEN,
            settings.TELEGRAM_CHAT_ID,
            status_provider=self._build_status_snapshot,
            controls=self,  # TradingApp implements set_paused/is_paused/set_muted/is_muted
        )
        self._started_at = time.time()
        # Telegram control state: /pause and /resume flip this; when paused the
        # trading cycle stops OPENING new positions but keeps managing existing
        # ones (early exits + settlement still run). In-memory only — a restart
        # always resumes unpaused, which is the safe default.
        self._trading_paused = False
        # Feed health is created before the feeds because the feeds' callbacks
        # reference it. It only tracks binance + polymarket (see
        # engine/feed_health.FEEDS) — coinbase feeds the cross-exchange gate
        # and is not part of the liveness gate.
        self.feed_health = FeedHealth()
        self.ws_feed = PolymarketWSFeed(
            asset_ids=[],
            on_message=lambda: self.feed_health.record_message("polymarket"),
            on_reconnect=lambda: self.feed_health.record_reconnect("polymarket"),
        )
        self.feed = PolymarketFeed(min_liquidity_usd=settings.MIN_MARKET_LIQUIDITY_USD, ws_feed=self.ws_feed)
        self.binance_feed = BinanceFeed(
            on_reconnect=lambda: self.feed_health.record_reconnect("binance"),
        )
        self.coinbase_feed = CoinbaseFeed()
        self.signal_engine: SignalEngine | None = None
        self.risk: RiskManager | None = None
        self.latency: LatencyTracker | None = None
        self.broker: Union[PaperBroker, LiveBroker, None] = None
        self._shutdown = asyncio.Event()
        self._dashboard_state = DashboardState()
        self._eval_semaphore = asyncio.Semaphore(MAX_CONCURRENT_MARKET_EVALS)
        # Serializes the position-opening critical section (has_open_position
        # check -> order placement) across BOTH entry paths — the 1s polling
        # cycle and the event-driven fast path — so they can never race to
        # open a second position on the same market.
        self._entry_lock = asyncio.Lock()
        # Per-symbol fast-path trigger state: the price and tick time at the
        # last fast evaluation, so the next trigger is a CUMULATIVE move from
        # there rather than a single-tick blip.
        self._fast_path_state: dict[str, dict[str, float]] = {}
        # Fast-path worker task + coalescing flag: the worker runs as a task so
        # a slow pass never stalls the Binance ingest loop; a trigger arriving
        # mid-pass just requests one more pass. _fast_path_last_update is the
        # freshest triggering tick — every pass (including reruns) measures
        # latency from it, never from the first trigger of a burst.
        self._fast_path_task: Optional[asyncio.Task] = None
        self._fast_path_rerun = False
        self._fast_path_last_update: Optional[PriceUpdate] = None

        # Empirical arbitrage-window measurement (pure diagnostics, never
        # gates trading): measures how long Polymarket takes to reprice after
        # a Binance move — the number ASSUMED_ARBITRAGE_WINDOW_S guesses at.
        self.lag_tracker = LagTracker(
            min_reprice_move=settings.LAG_REPRICE_MIN_MOVE,
            timeout_s=settings.LAG_TRACK_TIMEOUT_S,
        )
        # Previous ingested price per symbol — the per-tick move that the lag
        # tracker watches (independent of the fast path's cumulative trigger).
        self._last_ingest_price: dict[str, float] = {}

        # market_id -> Market, maintained by _market_discovery_loop, read (not
        # re-fetched) by the hot per-second trading cycle. This is what fixes
        # the "Gamma called every single second" inefficiency — discovery now
        # runs on its own, slower, independent schedule.
        self._known_markets: dict[str, Market] = {}
        # Markets with at least one open position, independent of whether
        # they're still "active" — this is what fixes the settlement dead-code
        # bug: discover_active_markets() filters to active=true,closed=false,
        # so a resolved market can never reappear there. Settlement checks
        # this set directly via get_market_by_id() instead.
        self._open_position_market_ids: set[str] = set()
        # LIVE-mode settlement: conditionIds for which a redeemPositions() tx
        # has already been submitted this process run. Prevents double-
        # broadcasting while the data-api lags the chain (a second redeem on
        # an already-burned condition reverts and wastes gas).
        self._redeem_submitted: set[str] = set()

    async def setup(self) -> None:
        await self.db.connect()
        calibration = load_calibration()
        if calibration:
            logger.info("Loaded calibration for horizons: %s minutes", sorted(calibration.keys()))
        else:
            logger.info("No calibration file found — using the uncalibrated momentum fallback where fair-value "
                        "inputs aren't available. Run scripts/calibrate_momentum_model.py when you can.")
        self.signal_engine = SignalEngine(settings, self.db, calibration=calibration)
        self.risk = RiskManager(settings, self.db, self.alerter)
        self.latency = LatencyTracker(self.db)
        self.broker = await get_broker(self.db, self.feed, self.alerter)

        restored = 0
        if isinstance(self.broker, PaperBroker):
            restored = await self.broker.load_open_positions()

        starting_balance = await self.broker.get_balance()
        await self.risk.load_state(starting_balance)

        mode_label = "PAPER" if isinstance(self.broker, PaperBroker) else "LIVE"
        self._dashboard_state.mode = mode_label
        # Populate the dashboard account panel immediately, so the UI shows the
        # real paper balance ($1000 by default) from the very first render
        # instead of the DashboardState default of $0.00 until the first
        # healthy trading cycle happens to write it.
        await self._refresh_dashboard_state()
        logger.info("Startup complete. Mode=%s. Restored %d open position(s) from a previous run.",
                    mode_label, restored)
        await self.alerter.send_alert(
            f"Bot starting up in {mode_label} mode. Restored {restored} open position(s).",
            level=AlertLevel.INFO,
        )

    async def _binance_ingest_loop(self) -> None:
        async for update in self.binance_feed.stream():
            self.signal_engine.ingest_price_update(update, source="binance")
            self.feed_health.record_message("binance")
            self._record_lag_moves(update)
            if self._shutdown.is_set():
                return
            try:
                await self._maybe_fast_path(update)
            except Exception:
                logger.exception("Fast-path trigger failed for %s", update.symbol)

    # -- Lag-gap measurement (instrumentation — never gates trading) ----------

    def _record_lag_moves(self, update: PriceUpdate) -> None:
        """
        Feed the lag tracker: when a Binance tick moves >= LAG_TRACK_MOVE_MIN_PCT
        from the previous tick for that symbol, start measuring how long the
        direction-implied Polymarket token of the most-liquid market for this
        asset takes to reprice. Pure diagnostics; best-effort.
        """
        try:
            last = self._last_ingest_price.get(update.symbol)
            self._last_ingest_price[update.symbol] = update.price
            if last is None or last <= 0:
                return
            move = (update.price - last) / last
            if abs(move) < settings.LAG_TRACK_MOVE_MIN_PCT:
                return
            asset = update.symbol[:-4] if update.symbol.endswith("USDT") else update.symbol
            market = self._most_liquid_market_for(asset)
            if market is None:
                return
            token_id = market.token_id_yes if move > 0 else market.token_id_no
            if not self.ws_feed.is_fresh(token_id):
                return
            book = self.ws_feed.get_cached_book(token_id)
            if book is None or book.mid is None:
                return
            self.lag_tracker.on_move(
                asset=asset, move_pct=abs(move),
                move_dir="UP" if move > 0 else "DOWN",
                token_id=token_id, baseline_mid=book.mid,
                ts=update.received_at,
            )
        except Exception:
            logger.debug("Lag tracking skipped for %s", update.symbol, exc_info=True)

    def _most_liquid_market_for(self, asset: str) -> Optional[Market]:
        """The known market for `asset` with the deepest liquidity — the one
        the bot would actually trade, so the lag measured is the tradable
        lag, not an illiquid side market."""
        candidates = [m for m in self._known_markets.values() if m.asset == asset]
        if not candidates:
            return None
        return max(candidates, key=lambda m: m.liquidity_usd or 0.0)

    async def _lag_tracker_loop(self) -> None:
        """
        Every LAG_TRACK_INTERVAL_S, scan the lag tracker's pending moves
        against the live WS books and persist finalized measurements (both
        repriced and timed-out) to the lag_events table. Independent of
        trading — a slow write must never stall a cycle.
        """
        while not self._shutdown.is_set():
            try:
                await self._lag_tracker_pass()
            except Exception:
                logger.exception("Lag tracker pass failed")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=settings.LAG_TRACK_INTERVAL_S)
            except asyncio.TimeoutError:
                continue

    async def _lag_tracker_pass(self) -> None:
        now = time.time()
        for token_id in self.lag_tracker.pending_token_ids():
            if not self.ws_feed.is_fresh(token_id):
                continue
            book = self.ws_feed.get_cached_book(token_id)
            if book is None or book.mid is None:
                continue
            measurement = self.lag_tracker.observe(token_id=token_id, mid=book.mid, ts=now)
            if measurement is not None:
                await self._persist_lag(measurement)
        for measurement in self.lag_tracker.sweep(ts=now):
            await self._persist_lag(measurement)

    async def _persist_lag(self, measurement: LagMeasurement) -> None:
        try:
            await self.db.log_lag_event(**measurement.to_db_row())
        except Exception:
            logger.exception("Failed to persist lag measurement for %s", measurement.asset)

    async def _coinbase_ingest_loop(self) -> None:
        """Coinbase ticks feed ONLY the cross-exchange sanity gate (they are
        recorded per-source in the engine but never enter the model's momentum
        tracker) — see SignalEngine.ingest_price_update's `source` param."""
        async for update in self.coinbase_feed.stream():
            self.signal_engine.ingest_price_update(update, source="coinbase")
            if self._shutdown.is_set():
                return

    async def _market_discovery_loop(self) -> None:
        """
        Runs independently of the (much faster) trading loop. Refreshes
        self._known_markets, captures each new market's reference price at
        first sighting (see Market.reference_price's docstring for the
        approximation this relies on), and keeps the WS feed's subscription
        list current. Markets that drop out of the active list are pruned
        from _known_markets UNLESS we still hold an open position in them —
        those stay until settled, so get_equity()/close_position_early() can
        still resolve their token IDs.
        """
        while not self._shutdown.is_set():
            try:
                markets = await self.feed.discover_active_markets()
                for m in markets:
                    existing = self._known_markets.get(m.market_id)
                    if existing is not None and existing.reference_price is not None:
                        m = m.with_reference_price(existing.reference_price)
                    elif m.market_id not in self._known_markets:
                        # Reference-price trust guard (verified 2026-08-07):
                        # the fair-value model needs the price at the market's
                        # OPEN. If discovery first sees a market late in its
                        # window (bot restarted mid-contract, or Gamma served
                        # it late), the Binance price now is NOT the open
                        # price — using it as the reference makes the model
                        # confidently wrong in direction (836 saturated reads
                        # like "model 2% vs market 99.5%" in one run). Only
                        # trust a first-sighting reference when the window is
                        # still mostly ahead of us. FAILS CLOSED: when the
                        # remaining time can't be computed (None), we cannot
                        # tell if the price is stale — so we don't trust it
                        # (reviewed 2026-08-07: the guard used to fall
                        # through to trusting in exactly that case).
                        remaining = m.time_remaining_s
                        duration_s = (m.duration_minutes or 15) * 60
                        if (
                            remaining is None
                            or remaining < duration_s * settings.REFERENCE_TRUST_MIN_REMAINING_PCT
                        ):
                            # Leave reference_price unset -> fair-value stays
                            # off; the calibrated momentum fallback (honestly
                            # ~52%) will refuse to invent an edge on stale
                            # inputs, so no phantom trade fires.
                            logger.debug(
                                "Late or unknown first sighting of %s (%s) — "
                                "reference price not trusted, fair-value stays off",
                                m.market_id,
                                f"{remaining:.0f}s of {duration_s}s left" if remaining is not None
                                else "remaining time unknown",
                            )
                        else:
                            ref_price = self.signal_engine.current_price(m.asset)
                            if ref_price is not None:
                                m = m.with_reference_price(ref_price)
                            else:
                                logger.debug("No Binance price yet to use as reference for new market %s", m.market_id)
                    self._known_markets[m.market_id] = m

                active_ids = {m.market_id for m in markets}
                for stale_id in list(self._known_markets.keys()):
                    if stale_id not in active_ids and stale_id not in self._open_position_market_ids:
                        del self._known_markets[stale_id]

                token_ids = []
                for m in self._known_markets.values():
                    token_ids.extend([m.token_id_yes, m.token_id_no])
                self.ws_feed.update_assets(token_ids)
            except Exception:
                logger.exception("Market discovery cycle failed")
            await asyncio.sleep(settings.MARKET_DISCOVERY_INTERVAL_S)

    async def _settlement_loop(self) -> None:
        """
        Independently polls resolution status for every market with an open
        position, using PolymarketFeed.get_market_by_id() — which, unlike
        discover_active_markets(), does NOT filter out closed/resolved
        markets. This is the fix for the settlement dead-code bug: the old
        code only ever checked `market.resolved` on markets pulled from the
        active-only discovery list, where resolved is always False by
        construction, so settle_position() could never actually fire.

        PAPER mode: settles open paper trades at the resolved outcome.
        LIVE mode: discovers open on-chain positions via the data-api and
        submits one redeemPositions() transaction per resolved market (the
        automated counterpart of scripts/manual_redeem.py).
        """
        while not self._shutdown.is_set():
            try:
                await self._settlement_pass()
            except Exception:
                logger.exception("Settlement pass failed")
            await asyncio.sleep(SETTLEMENT_POLL_INTERVAL_S)

    async def _settlement_pass(self) -> None:
        """One pass of settlement checks — extracted from _settlement_loop so
        tests can drive it directly without the infinite loop."""
        if isinstance(self.broker, PaperBroker):
            await self._settle_paper_positions()
        elif isinstance(self.broker, LiveBroker):
            await self._redeem_resolved_live_positions()

    async def _settle_paper_positions(self) -> None:
        """Paper-mode settlement: settle open trades at the resolved outcome."""
        open_trades = await self.db.get_open_trades(mode="PAPER")
        self._open_position_market_ids = {t["market_id"] for t in open_trades}

        for market_id in list(self._open_position_market_ids):
            try:
                market = self._known_markets.get(market_id) or await self.feed.get_market_by_id(market_id)
                if market is None:
                    continue
                if market.resolved:
                    pnl = await self.broker.settle_position(market)
                    if pnl is not None:
                        await self.alerter.send_alert(
                            f"[{self.broker.mode}] Settled {market_id}: PnL ${pnl:.2f}",
                            level=AlertLevel.INFO,
                        )
                        self._open_position_market_ids.discard(market_id)
            except Exception:
                logger.exception("Settlement check failed for market %s", market_id)

    async def _redeem_resolved_live_positions(self) -> None:
        """
        LIVE-mode settlement: pulls open on-chain positions from the public
        data-api (GET /positions?user=<wallet>, which returns only open
        positions) and submits one redeemPositions() transaction per
        redeemable, resolved market — the automated counterpart of
        scripts/manual_redeem.py. Only the wallet's own holdings are ever
        touched; nothing else is redeemed.

        Each position is pre-flighted against the data-api's own `redeemable`
        flag (only True once the condition can actually be redeemed) BEFORE
        the Gamma fetch, so losing or not-yet-resolved conditions never
        trigger a broadcast. Submitted conditionIds are tracked in
        self._redeem_submitted so a position is never broadcast twice within
        this process run (the data-api can lag the chain by
        seconds-to-a-minute; a second redeem on an already-burned condition
        reverts and wastes gas). A broadcast failure is NOT recorded, so it
        will be retried next pass; an on-chain revert (e.g. not truly
        resolved yet) won't be retried until the next restart — the
        conservative choice, since we don't wait for receipts here.
        """
        try:
            positions = await self.broker.get_open_positions(self.broker.wallet_address)
        except Exception:
            logger.exception("Live settlement: could not fetch positions from data-api")
            return

        for pos in positions:
            try:
                condition_id = str(pos.get("conditionId") or "").strip()
                if not condition_id or condition_id in self._redeem_submitted:
                    continue
                try:
                    size = float(pos.get("size") or 0)
                except (TypeError, ValueError):
                    size = 0.0
                if size <= 0:
                    continue  # nothing held in this condition — nothing to redeem
                if not pos.get("redeemable"):
                    continue  # data-api says this condition can't be redeemed yet

                market = self._known_markets.get(condition_id) or await self.feed.get_market_by_id(condition_id)
                if market is None or not market.resolved:
                    continue

                # Logging context only — LIVE trades have no DB row, so the
                # conditionId stands in for the trade id.
                trade = {"id": condition_id, "side": str(pos.get("outcome") or "").upper()}
                tx_hash = await self.broker.redeem_position(market, trade)
                self._redeem_submitted.add(condition_id)
                await self.alerter.send_alert(
                    f"[LIVE] Redeem submitted {condition_id} (size {size:.4f}): tx {tx_hash}",
                    level=AlertLevel.INFO,
                )
            except Exception:
                logger.exception("Live redemption failed for position %s", pos.get("conditionId"))

    async def _trading_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                await self._trading_cycle()
            except Exception:
                logger.exception("Unhandled error in trading cycle — continuing after backoff")
                await self.alerter.send_alert(
                    "Unhandled error in trading cycle (see logs). Continuing after backoff.",
                    level=AlertLevel.WARNING,
                )
            await asyncio.sleep(POLL_INTERVAL_S)

    async def _trading_cycle(self) -> None:
        # Refresh the dashboard account panel BEFORE the early-returns below,
        # so the UI always shows the real balance/PnL — never a stuck $0.00 —
        # even when this cycle is skipped (unhealthy feed, paused, halted).
        await self._refresh_dashboard_state()

        if not self.feed_health.is_healthy():
            # Never skip silently: an unhealthy feed (reconnect storm or stale
            # data) means the signals we'd evaluate are built on unreliable
            # inputs, so the whole cycle is skipped and the reason is logged.
            #
            # Deliberate side effect: because this returns before
            # risk.update() and the halt/kill-switch checks, equity tracking
            # pauses during a feed outage too. That is intentional — nothing
            # trades meanwhile, and risk catches up on the first healthy cycle
            # after recovery.
            logger.warning("Skipping trading cycle: reason=feed_unhealthy")
            return
        cash = await self.broker.get_balance()
        equity = cash
        total_exposure = 0.0
        if isinstance(self.broker, PaperBroker):
            equity = await self.broker.get_equity(self._known_markets)
            total_exposure = await self.broker.get_total_exposure_usd()

        await self.risk.update(equity)
        self._dashboard_state.daily_halted = self.risk.daily_halted
        self._dashboard_state.kill_switch_tripped = self.risk.kill_switch_tripped

        if not self.risk.is_trading_allowed():
            return  # halted or kill-switched — evaluate nothing new, just idle

        if self._trading_paused:
            # /pause: stop opening NEW positions, but still manage existing
            # ones (early exits + settlement run below/independently).
            logger.info("Skipping new entries: reason=trading_paused")
            await self._check_early_exits(equity)
            return

        markets = list(self._known_markets.values())
        if not markets:
            return

        exposure_cap_usd = settings.MAX_TOTAL_EXPOSURE_PCT * equity
        exposure_headroom = max(0.0, exposure_cap_usd - total_exposure)

        results = await asyncio.gather(
            *(self._evaluate_and_maybe_trade(market, cash, equity, exposure_headroom) for market in markets),
            return_exceptions=True,
        )
        for market, result in zip(markets, results):
            if isinstance(result, Exception):
                logger.exception("Unhandled error evaluating market %s", market.market_id, exc_info=result)

        await self._check_early_exits(equity)

    async def _evaluate_and_maybe_trade(
        self,
        market: Market,
        cash: float,
        equity: float,
        exposure_headroom: float,
        yes_book: Optional[OrderBook] = None,
        no_book: Optional[OrderBook] = None,
        tick_received_at: Optional[float] = None,
        book_source: Optional[object] = None,
    ) -> None:
        """
        Evaluate one market and open a position if a signal fires. Shared by
        BOTH entry paths:
          - the 1s polling cycle (books fetched over REST here)
          - the event-driven fast path (WS-cached books passed in, so the
            whole evaluation takes a few ms and the arbitrage window is
            winnable)

        The has_open_position -> place_order critical section is serialized
        by _entry_lock so the two paths can never race to double-enter a
        market.
        """
        if exposure_headroom <= 0:
            return
        async with self._eval_semaphore:
            # True latency start: the Binance tick that moved the price, not
            # this cycle's own start time. tick_to_order_ms measured from here
            # is the honest number the arbitrage-window verdict is built on.
            if tick_received_at is None:
                tick_received_at = self.signal_engine.latest_tick_received_at(market.asset) or time.time()
            cycle = self.latency.start(market.market_id, tick_received_at=tick_received_at)

            if yes_book is None or no_book is None:
                try:
                    yes_book = await self.feed.get_order_book(market.market_id, market.token_id_yes)
                    no_book = await self.feed.get_order_book(market.market_id, market.token_id_no)
                except Exception:
                    logger.debug("Could not fetch order books for %s, skipping this cycle", market.market_id)
                    return

            async with self._entry_lock:
                if isinstance(self.broker, PaperBroker) and self.broker.has_open_position(market.market_id):
                    return  # never stack a second position on a market we're already in

                # Exposure accounting must be re-checked UNDER the lock: the
                # headroom passed in was computed before it, and a concurrent
                # entry path (poll cycle vs fast path) may have opened a
                # position in the meantime. Without this, two overlapping
                # entries could each size against stale headroom and overshoot
                # MAX_TOTAL_EXPOSURE_PCT by up to one position (reviewed
                # 2026-08-07). One cheap SQLite read per entry attempt.
                if isinstance(self.broker, PaperBroker):
                    fresh_exposure = await self.broker.get_total_exposure_usd()
                    exposure_headroom = min(
                        exposure_headroom,
                        max(0.0, settings.MAX_TOTAL_EXPOSURE_PCT * equity - fresh_exposure),
                    )
                    if exposure_headroom <= 0:
                        return

                # Sum-to-one is checked first: it's risk-free (doesn't need a
                # directional forecast), so it doesn't compete with or get gated
                # by the directional signal below.
                if isinstance(self.broker, PaperBroker):
                    fee_pct = settings.TAKER_FEE_PCT
                    sto_opportunity = find_sum_to_one_opportunity(
                        market, yes_book, no_book, settings.SUM_TO_ONE_MIN_EDGE_PCT, fee_pct,
                    )
                    if sto_opportunity is not None:
                        sto_size = min(
                            settings.SUM_TO_ONE_MAX_POSITION_PCT * equity, exposure_headroom, cash,
                        )
                        if sto_size >= self.broker.min_order_size_usd * 2:
                            try:
                                yes_fill, no_fill = await self.broker.place_sum_to_one_order(
                                    sto_opportunity, sto_size, book_source=book_source,
                                )
                                await self.alerter.send_alert(
                                    f"[{self.broker.mode}] Sum-to-one {market.asset} ${sto_size:.2f} "
                                    f"(YES {yes_fill.avg_price:.3f} + NO {no_fill.avg_price:.3f}, "
                                    f"locked edge {sto_opportunity.net_profit_pct:.2%})",
                                    level=AlertLevel.INFO,
                                )
                            except SumToOneEdgeLostError as e:
                                # The combo edge vanished between detection and
                                # fill; both legs were already reversed by the
                                # broker. This is a normal market condition, not
                                # a bug — log at INFO, never alert.
                                logger.info("Sum-to-one %s skipped: %s", market.market_id, e)
                            except Exception:
                                logger.exception("Sum-to-one order failed for market %s", market.market_id)
                        await self.latency.finish(cycle, fired=True)
                        return  # don't also evaluate the directional signal this cycle

                signal = await self.signal_engine.evaluate(market, yes_book, no_book)
                cycle.mark_signal_evaluated()

                if not signal.fired:
                    await self.latency.finish(cycle, fired=False)
                    return

                size_usd = self.risk.position_size(
                    SignalForSizing(edge_pct=signal.edge_pct, entry_price=yes_book.mid or 0.5),
                    current_balance=equity,
                )
                size_usd = min(size_usd, cash, exposure_headroom)
                min_size = getattr(self.broker, "min_order_size_usd", 0.0)
                if size_usd < min_size:
                    await self.latency.finish(cycle, fired=True)
                    return

                try:
                    kwargs = {"book_source": book_source} if book_source is not None else {}
                    if book_source is not None:
                        # Analysis label only (no behavior change): mark
                        # event-driven fast-path entries so the validation run
                        # can split PnL / win rate by entry path. book_source
                        # is only ever set on the fast path, and only for
                        # PaperBroker (LiveBroker gets book_source_arg=None).
                        kwargs["strategy"] = "latency_arb_fast"
                    fill = await self.broker.place_order(market, signal.side, size_usd, **kwargs)
                    cycle.mark_order_submitted()
                    await self.alerter.send_alert(
                        f"[{self.broker.mode}] Opened {signal.side} {market.asset} "
                        f"${size_usd:.2f} @ {fill.avg_price:.3f} "
                        f"(edge {signal.edge_pct:.2%}, model={signal.model_used})",
                        level=AlertLevel.INFO,
                    )
                except Exception:
                    logger.exception("Order placement failed for market %s", market.market_id)
                finally:
                    await self.latency.finish(cycle, fired=True)

    # -- Event-driven fast path (win-the-gap) -------------------------------

    async def _maybe_fast_path(self, update: PriceUpdate) -> None:
        """
        Trigger gate for the event-driven fast path, called on EVERY Binance
        tick (pure arithmetic, microseconds). Runs the fast path only when the
        cumulative move since the last fast evaluation for this symbol clears
        FAST_PATH_MOVE_TRIGGER_PCT and the per-asset cooldown has elapsed —
        otherwise the bot would thrash on every micro-tick.

        The actual evaluation is dispatched to a coalescing worker TASK rather
        than awaited inline, so a slow run can never stall the Binance ingest
        loop (and with it every subsequent tick). If a new trigger arrives
        while the worker is busy, it runs one more pass with the freshest
        state when the current pass finishes.
        """
        symbol = update.symbol
        state = self._fast_path_state.get(symbol)
        if state is not None and not fast_path_should_run(
            last_price=state["price"], price=update.price,
            last_ts=state["ts"], received_at=update.received_at,
            trigger_pct=settings.FAST_PATH_MOVE_TRIGGER_PCT,
            cooldown_s=settings.FAST_PATH_COOLDOWN_S,
        ):
            return
        # Remember what we evaluated at, so the NEXT trigger is a cumulative
        # move from here — not from the previous tick.
        self._fast_path_state[symbol] = {"price": update.price, "ts": update.received_at}
        self._fast_path_last_update = update
        if self._fast_path_task is not None and not self._fast_path_task.done():
            self._fast_path_rerun = True
            return
        self._fast_path_rerun = False
        self._fast_path_task = asyncio.create_task(
            self._fast_path_worker(), name="fast_path_worker"
        )

    async def _fast_path_worker(self) -> None:
        """Coalescing worker: run the fast path, then one more pass if newer
        triggers arrived while it was busy (bounded: at most one rerun per
        burst). Each pass evaluates the freshest triggering tick's asset, so
        latency is measured from the actual move, not the first tick of a
        burst. Errors are logged, never raised into the ingest loop."""
        try:
            while True:
                update = self._fast_path_last_update
                if update is None:
                    return
                await self._run_fast_path(update)
                if not self._fast_path_rerun:
                    return
                self._fast_path_rerun = False
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Fast-path worker failed")

    async def _run_fast_path(self, update: PriceUpdate) -> None:
        """
        Evaluate every known market for the moved asset IMMEDIATELY — no 1s
        poll, no REST book fetches — against the continuously-updated
        Polymarket WS book cache. This is what actually wins the arbitrage
        window: the gap between a Binance move and our order drops from
        ~1.5-1.9s (poll + REST + fill latency) to ~0.3-0.6s.
        """
        if self._shutdown.is_set() or self._trading_paused:
            return
        if not (self.risk is not None and self.risk.is_trading_allowed()):
            return
        if not self.feed_health.is_healthy():
            return

        asset = update.symbol[:-4] if update.symbol.endswith("USDT") else update.symbol
        markets = [
            m for m in self._known_markets.values()
            if m.asset == asset
            and self.ws_feed.is_fresh(m.token_id_yes)
            and self.ws_feed.is_fresh(m.token_id_no)
        ]
        if not markets:
            return

        cash = await self.broker.get_balance()
        equity = cash
        total_exposure = 0.0
        if isinstance(self.broker, PaperBroker):
            equity = await self.broker.get_equity(self._known_markets)
            total_exposure = await self.broker.get_total_exposure_usd()
        headroom = max(0.0, settings.MAX_TOTAL_EXPOSURE_PCT * equity - total_exposure)
        if headroom <= 0:
            return

        # The WS book source is a PAPER-mode optimization: the paper broker's
        # place_order fills against it instead of a REST round trip. LiveBroker
        # fills against the real CLOB and its place_order takes no such kwarg —
        # passing it would TypeError every live fast-path order (reviewed
        # 2026-08-07). Gate it on the broker type.
        def book_source(token_id: str):
            return self.ws_feed.get_cached_book(token_id)

        book_source_arg = book_source if isinstance(self.broker, PaperBroker) else None
        for m in markets:
            yes_book = self.ws_feed.get_cached_book(m.token_id_yes)
            no_book = self.ws_feed.get_cached_book(m.token_id_no)
            if yes_book is None or no_book is None:
                continue
            try:
                await self._evaluate_and_maybe_trade(
                    m, cash, equity, headroom,
                    yes_book=yes_book, no_book=no_book,
                    tick_received_at=update.received_at,
                    book_source=book_source_arg,
                )
            except Exception:
                logger.exception("Fast-path evaluation failed for market %s", m.market_id)

    async def _check_early_exits(self, equity: float) -> None:
        """
        Without this, positions could only ever be held to expiry. Checks
        every open position each cycle for:
          - reprice (round-trip protocol): the market has repriced toward the
            entry side within the arbitrage window — bank the convergence
            gain and free the capital for the next trade. This is the piece
            that gives the strategy its high win rate (a bet the market
            CORRECTS, not a bet on the final outcome).
          - take-profit: current mark-to-market value has gained at least
            TAKE_PROFIT_PCT of stake
          - edge reversal: our own model's current read on this market has
            flipped to the OTHER side with a large enough edge that holding
            no longer agrees with our own signal
        """
        if not isinstance(self.broker, PaperBroker):
            return

        open_trades = await self.db.get_open_trades(mode="PAPER")
        for t in open_trades:
            # Sum-to-one legs are outcome-agnostic by construction (we hold
            # BOTH sides to settlement; whichever wins pays $1, and the combo
            # locked a profit below that). The directional model's opinion is
            # meaningless for them, so REPRICE / TAKE_PROFIT / EDGE_REVERSAL
            # must NEVER fire on a sum_to_one leg — exiting one leg early
            # breaks the hedge. Verified 2026-08-07: the model "reversed" the
            # NO leg of an ETH combo and sold it at 0.854 when holding to
            # settlement would have paid 1.0 — turning a guaranteed win into
            # a loss.
            if (t.get("strategy") or "latency_arb") == "sum_to_one":
                continue
            market = self._known_markets.get(t["market_id"])
            if market is None or not t["entry_price"]:
                continue

            token_id = market.token_id_yes if t["side"] == "YES" else market.token_id_no
            # Prefer the WS-cached book (zero-latency read, same source the
            # fast path fills against) so a round-trip exit is as fast as the
            # entry that triggered it; fall back to REST when the cache isn't
            # fresh. The round-trip protocol only works if the exit doesn't
            # eat the window the entry just won. Note the source split: this
            # DECISION reads the cache, but the actual SELL
            # (close_position_early) walks the REST book — in paper mode both
            # are the same live market and REST is authoritative for the
            # fill; the cache only decides WHEN to attempt the exit.
            book = None
            try:
                if self.ws_feed.is_fresh(token_id):
                    book = self.ws_feed.get_cached_book(token_id)
            except Exception:
                book = None
            if book is None:
                try:
                    book = await self.feed.get_order_book(market.market_id, token_id)
                except Exception:
                    continue
            if book.mid is None:
                continue

            unrealized_pct = (book.mid - t["entry_price"]) / t["entry_price"]

            # Round-trip protocol (the missing piece — see the AdiiX article
            # and REPRICE_EXIT_* settings): while the position is still inside
            # the reprice window, exit the moment the held token has gained
            # >= REPRICE_EXIT_GAIN_PCT from entry. This is a bet that the
            # market CORRECTS toward the side we bought (near-certain), not a
            # bet on the final outcome (a coin flip — our own calibration
            # measured ~52%). Holding to settlement was silently converting
            # every good lag entry into an EV-negative outcome bet; the
            # round-trip both raises the win rate and frees capital for the
            # next of hundreds of daily entries. After REPRICE_EXIT_MAX_HOLD_S
            # the arbitrage is gone, so the exit stops applying and the
            # normal exits take over.
            entry_ts = t.get("entry_ts") or 0.0
            held_s = time.time() - entry_ts
            if (
                held_s <= settings.REPRICE_EXIT_MAX_HOLD_S
                and unrealized_pct >= settings.REPRICE_EXIT_GAIN_PCT
            ):
                await self._try_early_exit(market, t["id"], "REPRICE")
                continue

            if unrealized_pct >= settings.TAKE_PROFIT_PCT:
                await self._try_early_exit(market, t["id"], "TAKE_PROFIT")
                continue

            try:
                yes_book = await self.feed.get_order_book(market.market_id, market.token_id_yes)
                no_book = await self.feed.get_order_book(market.market_id, market.token_id_no)
                current_signal = await self.signal_engine.evaluate(market, yes_book, no_book, log=False)
            except Exception:
                continue

            if (
                current_signal.side
                and current_signal.side != t["side"]
                and current_signal.edge_pct >= settings.EDGE_REVERSAL_EXIT_THRESHOLD_PCT
            ):
                await self._try_early_exit(market, t["id"], "EDGE_REVERSAL")

    async def _try_early_exit(self, market: Market, trade_id: int, reason: str) -> None:
        try:
            pnl = await self.broker.close_position_early(market, trade_id, reason=reason)
            if pnl is not None:
                await self.alerter.send_alert(
                    f"[{self.broker.mode}] Early exit ({reason}) {market.market_id}: PnL ${pnl:.2f}",
                    level=AlertLevel.INFO,
                )
        except Exception:
            logger.exception("Early exit failed for market %s trade %d", market.market_id, trade_id)

    async def _refresh_dashboard_state(self) -> None:
        """
        Populate the full DashboardState (balance, PnL, win rate, positions,
        recent trades, feed health, risk flags) from the broker + DB. Called at
        startup and at the TOP of every trading cycle — BEFORE the feed-health
        gate — so the dashboard never shows a misleading $0.00 balance just
        because the current cycle happened to be skipped (unhealthy feed,
        paused, etc.). A $0 balance shown during an outage was a real bug: the
        paper account holds $1000 but the UI reported nothing.
        """
        self._dashboard_state.binance_feed_healthy = self.feed_health.is_feed_healthy("binance")
        self._dashboard_state.polymarket_feed_healthy = self.feed_health.is_feed_healthy("polymarket")
        if self.risk:
            self._dashboard_state.daily_halted = self.risk.daily_halted
            self._dashboard_state.kill_switch_tripped = self.risk.kill_switch_tripped
        try:
            cash = await self.broker.get_balance()
            self._dashboard_state.balance_usd = cash
        except Exception:
            logger.exception("Dashboard refresh: could not read balance")

        try:
            trades = await self.db.get_all_trades(mode=self.broker.mode)
        except Exception:
            logger.exception("Dashboard refresh: could not read trades")
            return
        closed = [t for t in trades if t.get("status") == "CLOSED"]
        pnls = [t.get("realized_pnl_usd") or 0.0 for t in closed]
        wins = sum(1 for p in pnls if p > 0)
        self._dashboard_state.total_pnl_usd = sum(pnls)
        self._dashboard_state.win_rate_pct = (wins / len(pnls) * 100.0) if pnls else 0.0
        self._dashboard_state.open_positions = [
            {
                "market_id": t.get("market_id"),
                "side": t.get("side"),
                "entry_price": t.get("entry_price"),
                "size_usd": t.get("size_usd"),
            }
            for t in trades
            if t.get("status") == "OPEN"
        ]
        self._dashboard_state.last_trades = sorted(
            closed, key=lambda t: t.get("exit_ts") or t.get("entry_ts") or 0, reverse=True,
        )[:10]

    # -- Telegram control commands -----------------------------------------

    def is_paused(self) -> bool:
        return self._trading_paused

    def is_muted(self) -> bool:
        return self.alerter.muted

    def set_muted(self, muted: bool) -> str:
        self.alerter.set_muted(muted)
        state = "MUTED — routine alerts will be logged locally but not sent (CRITICAL always delivered)."
        if not muted:
            state = "UNMUTED — alerts are being delivered again."
        return f"Alerts {state}"

    async def set_paused(self, paused: bool) -> str:
        """
        /pause and /resume handler. Pausing stops OPENING new positions but
        keeps managing existing ones (early exits + settlement still run), so
        a pause never strands a book of open trades.
        """
        self._trading_paused = paused
        msg = (
            "Trading PAUSED — no new positions will be opened. "
            "Existing positions are still managed (early exits + settlement)."
            if paused
            else "Trading RESUMED — new positions will be opened again."
        )
        await self.alerter.send_alert(
            f"Trading {'paused' if paused else 'resumed'} via Telegram.",
            level=AlertLevel.WARNING if paused else AlertLevel.INFO,
        )
        return msg

    def _get_dashboard_state(self) -> DashboardState:
        return self._dashboard_state

    async def _build_status_snapshot(self) -> dict:
        """
        Everything the Telegram status digest shows, gathered in one place.
        Returns plain data (no formatting — that's alerts/status_report.py's
        job). Every read is best-effort: a failure in one stat degrades that
        field instead of killing the digest, because a status message must
        never be the thing that crashes the trading loop.
        """
        mode = self._dashboard_state.mode
        balance_usd = 0.0
        equity_usd = 0.0
        try:
            balance_usd = await self.broker.get_balance()
            if isinstance(self.broker, PaperBroker):
                equity_usd = await self.broker.get_equity(self._known_markets)
            else:
                equity_usd = balance_usd
        except Exception:
            logger.exception("Status snapshot: could not read balance/equity")

        trades: list[dict] = []
        try:
            trades = await self.db.get_all_trades(mode=self.broker.mode)
        except Exception:
            logger.exception("Status snapshot: could not read trades")

        closed = [t for t in trades if t.get("status") == "CLOSED"]
        open_positions = [t for t in trades if t.get("status") == "OPEN"]
        pnls = [t.get("realized_pnl_usd") or 0.0 for t in closed]
        total_pnl = sum(pnls)
        wins = sum(1 for p in pnls if p > 0)
        win_rate_pct = (wins / len(pnls) * 100.0) if pnls else None

        by_strategy: dict[str, dict] = {}
        for t in closed:
            strat = t.get("strategy") or "latency_arb"
            bucket = by_strategy.setdefault(strat, {"trades": 0, "pnl_usd": 0.0, "wins": 0})
            bucket["trades"] += 1
            bucket["pnl_usd"] += t.get("realized_pnl_usd") or 0.0
            if (t.get("realized_pnl_usd") or 0.0) > 0:
                bucket["wins"] += 1
        for bucket in by_strategy.values():
            bucket["win_rate_pct"] = (
                bucket["wins"] / bucket["trades"] * 100.0 if bucket["trades"] else None
            )

        recent_trades = sorted(
            closed, key=lambda t: t.get("exit_ts") or t.get("entry_ts") or 0, reverse=True,
        )[:6]

        uptime_s = int(time.time() - self._started_at)
        days, rem = divmod(uptime_s, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        uptime = f"{days}d {hours:02d}h {minutes:02d}m"

        # LIVE trades have no DB row (they exist on-chain; see the settlement
        # loop), so per-trade stats would silently read as zeros in live mode.
        # Say so explicitly instead of showing a misleading empty report.
        stats_note = ""
        if mode == "LIVE":
            stats_note = (
                "LIVE mode: per-trade stats are tracked on-chain, not in the "
                "local DB, so the trade/strategy sections below show zeros."
            )

        paused = self._trading_paused
        alerts_muted = self.alerter.muted if self.alerter else False

        # Feed detail for the CRM: reconnect counts + seconds since last
        # message, separately per feed.
        feed_detail = {}
        for feed in ("binance", "polymarket"):
            feed_detail[feed] = {
                "reconnects_10m": self.feed_health.reconnect_count(feed),
                "stale_s": self.feed_health.seconds_since_last_message(feed),
            }

        # Risk detail: real numbers, not just booleans.
        risk_detail = {}
        if self.risk:
            risk_detail = {
                "daily_pnl_pct": self.risk.daily_pnl_pct,
                "drawdown_pct": self.risk.drawdown_pct,
                "daily_halt_threshold_pct": settings.DAILY_LOSS_HALT_PCT,
                "kill_threshold_pct": settings.TOTAL_DRAWDOWN_KILL_PCT,
            }

        latency = await self._build_latency_summary()
        config = self._build_config_summary()

        # Empirical Polymarket repricing lag (measured, not assumed). Empty
        # until the lag tracker has collected a few moves.
        lag = {}
        try:
            lag_events = await self.db.get_lag_events()
            if lag_events:
                lags = sorted(e["lag_ms"] for e in lag_events if e.get("lag_ms") is not None)
                if lags:
                    lag = {
                        "measured": len(lags),
                        "timed_out": sum(1 for e in lag_events if e.get("timed_out")),
                        "lag_p50_ms": lags[len(lags) // 2],
                        "lag_p95_ms": lags[min(len(lags) - 1, int(len(lags) * 0.95))],
                    }
        except Exception:
            logger.exception("Status snapshot: could not read lag events")

        return {
            "mode": mode,
            "lag": lag,
            "stats_note": stats_note,
            "balance_usd": balance_usd,
            "equity_usd": equity_usd,
            "total_pnl_usd": total_pnl,
            "win_rate_pct": win_rate_pct,
            "closed_trades": len(closed),
            "open_positions": len(open_positions),
            "uptime": uptime,
            "paused": paused,
            "alerts_muted": alerts_muted,
            "binance_feed_healthy": self.feed_health.is_feed_healthy("binance"),
            "polymarket_feed_healthy": self.feed_health.is_feed_healthy("polymarket"),
            "daily_halted": self.risk.daily_halted if self.risk else False,
            "kill_switch_tripped": self.risk.kill_switch_tripped if self.risk else False,
            "feed_detail": feed_detail,
            "risk_detail": risk_detail,
            "latency": latency,
            "config": config,
            "positions": [
                {
                    "market_id": t.get("market_id"),
                    "side": t.get("side"),
                    "size_usd": t.get("size_usd"),
                    "entry_price": t.get("entry_price"),
                }
                for t in open_positions[:8]
            ],
            "recent_trades": [
                {
                    "market_id": t.get("market_id"),
                    "side": t.get("side"),
                    "realized_pnl_usd": t.get("realized_pnl_usd"),
                    "exit_reason": t.get("exit_reason") or t.get("status"),
                }
                for t in recent_trades
            ],
            "by_strategy": by_strategy,
        }

    async def _build_latency_summary(self) -> dict:
        """
        Latency percentiles from the DB plus the platform-imposed taker
        delay, compared against the assumed arbitrage window. All reads are
        best-effort — an empty table yields an empty dict, which the CRM
        formatter renders as "no latency data yet".
        """
        try:
            events = await self.db.get_latency_events()
        except Exception:
            logger.exception("Status snapshot: could not read latency events")
            return {}
        tick_to_signal = [e["tick_to_signal_ms"] for e in events if e.get("tick_to_signal_ms") is not None]
        tick_to_order = [e["tick_to_order_ms"] for e in events if e.get("tick_to_order_ms") is not None]
        if not tick_to_order and not tick_to_signal:
            return {}

        def pct(vals, p):
            if not vals:
                return None
            s = sorted(vals)
            k = (len(s) - 1) * p
            f, c = int(k), min(int(k) + 1, len(s) - 1)
            return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)

        window_s = settings.ASSUMED_ARBITRAGE_WINDOW_S
        platform_delay_ms = settings.PLATFORM_TAKER_DELAY_MS
        p95_order = pct(tick_to_order, 0.95) if tick_to_order else None
        verdict = "n/a"
        if p95_order is not None:
            total_ms = p95_order + platform_delay_ms
            window_ms = window_s * 1000
            if total_ms < window_ms * 0.5:
                verdict = "comfortable"
            elif total_ms < window_ms:
                verdict = "tight"
            else:
                verdict = "too slow"
        return {
            "tick_to_signal_p50_ms": pct(tick_to_signal, 0.50),
            "tick_to_signal_p95_ms": pct(tick_to_signal, 0.95),
            "tick_to_order_p50_ms": pct(tick_to_order, 0.50),
            "tick_to_order_p95_ms": p95_order,
            "platform_delay_ms": platform_delay_ms,
            "window_s": window_s,
            "cycles": len(events),
            "verdict": verdict,
        }

    def _build_config_summary(self) -> dict:
        """Key settings the CRM /config command shows. Strings only — keeps the
        formatter trivial and avoids leaking anything sensitive (the private
        key is excluded from Settings repr entirely, and not listed here)."""
        return {
            "mode": "PAPER" if isinstance(self.broker, PaperBroker) else "LIVE",
            "starting_balance_usd": f"${settings.STARTING_PAPER_BALANCE_USD:,.0f}",
            "edge_threshold_pct": f"{settings.EDGE_THRESHOLD_PCT:.2%}",
            "min_confidence": f"{settings.MIN_CONFIDENCE:.2f}",
            "min_market_liquidity_usd": f"${settings.MIN_MARKET_LIQUIDITY_USD:,.0f}",
            "max_position_pct": f"{settings.MAX_POSITION_PCT:.0%}",
            "max_total_exposure_pct": f"{settings.MAX_TOTAL_EXPOSURE_PCT:.0%}",
            "fresh_move_min_pct": f"{settings.FRESH_MOVE_MIN_PCT:.2%} in {settings.FRESH_MOVE_LOOKBACK_S:.0f}s",
            "min_entry_time_remaining_s": f"{settings.MIN_ENTRY_TIME_REMAINING_S:.0f}s",
            "fast_path_trigger_pct": f"{settings.FAST_PATH_MOVE_TRIGGER_PCT:.2%} since last eval",
            "reprice_exit_gain_pct": f"{settings.REPRICE_EXIT_GAIN_PCT:.0%} token gain",
            "reprice_exit_max_hold_s": f"{settings.REPRICE_EXIT_MAX_HOLD_S:.0f}s",
            "daily_loss_halt_pct": f"{settings.DAILY_LOSS_HALT_PCT:.0%}",
            "drawdown_kill_pct": f"{settings.TOTAL_DRAWDOWN_KILL_PCT:.0%}",
            "sum_to_one_min_edge_pct": f"{settings.SUM_TO_ONE_MIN_EDGE_PCT:.2%}",
            "simulated_fill_latency_s": f"{settings.SIMULATED_FILL_LATENCY_S:.1f}s",
            "arb_window_s": f"{settings.ASSUMED_ARBITRAGE_WINDOW_S:.1f}s",
            "platform_taker_delay_ms": f"{settings.PLATFORM_TAKER_DELAY_MS:.0f}ms",
            "telegram_digest_interval_h": f"{settings.TELEGRAM_STATUS_INTERVAL_HOURS:.0f}h",
        }

    async def _export_command_center_state(self) -> None:
        """
        Write the live snapshot consumed by the Command Center API
        (command_center/api/live_state.json — read by the FastAPI server every
        ~2s and streamed to the web UI). Pure side-effect and strictly
        best-effort: a failure here must never affect trading.
        """
        try:
            snapshot = await self._build_status_snapshot()
            now = time.time()

            markets: list[dict] = []
            for m in self._known_markets.values():
                yes_mid = no_mid = None
                try:
                    if self.ws_feed.is_fresh(m.token_id_yes):
                        book = self.ws_feed.get_cached_book(m.token_id_yes)
                        yes_mid = book.mid if book else None
                    if self.ws_feed.is_fresh(m.token_id_no):
                        book = self.ws_feed.get_cached_book(m.token_id_no)
                        no_mid = book.mid if book else None
                except Exception:
                    pass
                markets.append({
                    "market_id": m.market_id,
                    "asset": m.asset,
                    "duration_minutes": m.duration_minutes,
                    "question": m.question,
                    "liquidity_usd": m.liquidity_usd,
                    "expires_at_ts": m.expires_at_ts,
                    "time_remaining_s": m.time_remaining_s,
                    "reference_price": m.reference_price,
                    "yes_mid": yes_mid,
                    "no_mid": no_mid,
                })

            positions: list[dict] = []
            try:
                open_trades = await self.db.get_open_trades(mode=self.broker.mode)
            except Exception:
                open_trades = []
            for t in open_trades:
                market = self._known_markets.get(t["market_id"])
                token_id = None
                if market is not None:
                    token_id = market.token_id_yes if t["side"] == "YES" else market.token_id_no
                mark = None
                if token_id is not None:
                    try:
                        if self.ws_feed.is_fresh(token_id):
                            book = self.ws_feed.get_cached_book(token_id)
                            mark = book.mid if book else None
                    except Exception:
                        pass
                entry = t.get("entry_price") or 0.0
                shares = (t.get("size_usd") or 0.0) / entry if entry else 0.0
                positions.append({
                    "market_id": t.get("market_id"),
                    "trade_id": t.get("id"),
                    "side": t.get("side"),
                    "asset": t.get("asset"),
                    "entry_price": entry,
                    "size_usd": t.get("size_usd"),
                    "fee_usd": t.get("fee_usd"),
                    "strategy": t.get("strategy"),
                    "mark_price": mark or entry,
                    "unrealized_pnl_usd": ((mark or entry) - entry) * shares if entry else 0.0,
                })

            payload = {
                "exported_at": now,
                "uptime_s": int(now - self._started_at),
                **snapshot,
                "markets": markets,
                "positions": positions,
            }
            path = Path(__file__).resolve().parent / "command_center" / "api" / "live_state.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
            tmp.replace(path)  # atomic-ish swap so readers never see a half-written file
        except Exception:
            logger.exception("Command center state export failed")

    async def _command_center_state_loop(self) -> None:
        """Every ~2s, export the live snapshot for the Command Center API.
        Independent of trading — a slow export must never block a cycle."""
        while not self._shutdown.is_set():
            await self._export_command_center_state()
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                continue

    async def _telegram_status_loop(self) -> None:
        """
        Periodic Telegram digest: one immediately on startup (confirms the
        wiring), then every TELEGRAM_STATUS_INTERVAL_HOURS. Sends the full
        HTML CRM dashboard, not just the plain-text summary. Deliberately
        swallow-and-log — a digest failure must never affect trading.
        """
        if not self.telegram_reporter.enabled:
            return
        interval_s = max(60.0, settings.TELEGRAM_STATUS_INTERVAL_HOURS * 3600.0)
        while not self._shutdown.is_set():
            try:
                await self.telegram_reporter.send_crm_digest()
            except Exception:
                logger.exception("Telegram status digest failed; continuing")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval_s)
            except asyncio.TimeoutError:
                continue

    async def _telegram_command_loop(self) -> None:
        """
        PTB polling for /status /stats /help inside the app's own event loop.
        If the command listener can't start, trading must not care — log and
        keep running (alerts still work; only on-demand queries are lost).
        """
        try:
            await self.telegram_reporter.run_command_listener(self._shutdown)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Telegram command listener failed; continuing without it")

    async def run(self) -> None:
        await self.setup()

        loop = asyncio.get_running_loop()
        for sig_name in ("SIGINT", "SIGTERM"):
            if not hasattr(signal, sig_name):
                continue
            try:
                loop.add_signal_handler(getattr(signal, sig_name), self._shutdown.set)
            except NotImplementedError:
                # Windows: asyncio's event loop cannot register signal
                # handlers; Ctrl+C still raises KeyboardInterrupt, which the
                # `__main__` block at the bottom of this file handles. systemd
                # (Linux, see deploy/) gets the real SIGTERM handler.
                logger.warning(
                    "Signal-based graceful shutdown not supported on %s — "
                    "Ctrl+C (KeyboardInterrupt) will still stop the bot cleanly.",
                    sys.platform,
                )
                break

        tasks = [
            asyncio.create_task(self._binance_ingest_loop(), name="binance_ingest"),
            asyncio.create_task(self._coinbase_ingest_loop(), name="coinbase_ingest"),
            asyncio.create_task(self.ws_feed.run(), name="polymarket_ws_feed"),
            asyncio.create_task(self._market_discovery_loop(), name="market_discovery"),
            asyncio.create_task(self._settlement_loop(), name="settlement"),
            asyncio.create_task(self._trading_loop(), name="trading_loop"),
            asyncio.create_task(run_dashboard(self._get_dashboard_state), name="dashboard"),
            asyncio.create_task(self._command_center_state_loop(), name="command_center_state"),
            asyncio.create_task(self._lag_tracker_loop(), name="lag_tracker"),
        ]
        if self.telegram_reporter.enabled:
            tasks.append(asyncio.create_task(self._telegram_status_loop(), name="telegram_status"))
            if settings.TELEGRAM_COMMANDS_ENABLED:
                tasks.append(asyncio.create_task(self._telegram_command_loop(), name="telegram_commands"))

        await self._shutdown.wait()
        logger.info("Shutdown signal received, cancelling tasks...")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.feed.aclose()
        await self.db.close()
        logger.info("Shutdown complete.")


async def main() -> None:
    app = TradingApp()
    await app.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
