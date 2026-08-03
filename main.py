"""
polymarket-arb-bot entry point.

Run with: python main.py

There is exactly one function in this file that decides paper vs. live —
get_broker() — and it is not something any other code path bypasses.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from typing import Union

from alerts.telegram import AlertLevel, build_alerter
from config.settings import settings
from data.binance_feed import BinanceFeed
from data.coinbase_feed import CoinbaseFeed
from data.polymarket_feed import Market, PolymarketFeed
from data.polymarket_ws_feed import PolymarketWSFeed
from engine.broker_live import LiveBroker, LiveTradingNotEnabledError, build_live_broker
from engine.broker_paper import PaperBroker
from engine.calibration import load_calibration
from engine.feed_health import FeedHealth
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
            simulated_fill_latency_s=settings.SIMULATED_FILL_LATENCY_S,
            min_order_size_usd=settings.MIN_ORDER_SIZE_USD,
            tick_size=settings.TICK_SIZE,
        )


class TradingApp:
    def __init__(self):
        self.db = Database(settings.DATABASE_PATH)
        self.alerter = build_alerter(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID)
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
            if self._shutdown.is_set():
                return

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
        if not self.feed_health.is_healthy():
            # Never skip silently: an unhealthy feed (reconnect storm or stale
            # data) means the signals we'd evaluate are built on unreliable
            # inputs, so the whole cycle is skipped and the reason is logged.
            #
            # Deliberate side effect: because this returns before
            # risk.update() and the dashboard-state writes, equity tracking
            # and halt/kill-switch detection pause during a feed outage too.
            # That is intentional — nothing trades meanwhile, and risk catches
            # up on the first healthy cycle after recovery.
            logger.warning("Skipping trading cycle: reason=feed_unhealthy")
            return
        cash = await self.broker.get_balance()
        equity = cash
        total_exposure = 0.0
        if isinstance(self.broker, PaperBroker):
            equity = await self.broker.get_equity(self._known_markets)
            total_exposure = await self.broker.get_total_exposure_usd()

        await self.risk.update(equity)
        self._dashboard_state.balance_usd = cash
        self._dashboard_state.daily_halted = self.risk.daily_halted
        self._dashboard_state.kill_switch_tripped = self.risk.kill_switch_tripped

        if not self.risk.is_trading_allowed():
            return  # halted or kill-switched — evaluate nothing new, just idle

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

    async def _evaluate_and_maybe_trade(self, market: Market, cash: float, equity: float, exposure_headroom: float) -> None:
        async with self._eval_semaphore:
            if isinstance(self.broker, PaperBroker) and self.broker.has_open_position(market.market_id):
                return  # never stack a second position on a market we're already in
            if exposure_headroom <= 0:
                return

            tick_received_at = time.time()
            cycle = self.latency.start(market.market_id, tick_received_at=tick_received_at)

            try:
                yes_book = await self.feed.get_order_book(market.market_id, market.token_id_yes)
                no_book = await self.feed.get_order_book(market.market_id, market.token_id_no)
            except Exception:
                logger.debug("Could not fetch order books for %s, skipping this cycle", market.market_id)
                return

            # Sum-to-one is checked first: it's risk-free (doesn't need a
            # directional forecast), so it doesn't compete with or get gated
            # by the directional signal below.
            if isinstance(self.broker, PaperBroker):
                fee_pct = getattr(self.broker, "fee_pct", 0.02)
                sto_opportunity = find_sum_to_one_opportunity(
                    market, yes_book, no_book, settings.SUM_TO_ONE_MIN_EDGE_PCT, fee_pct,
                )
                if sto_opportunity is not None:
                    sto_size = min(
                        settings.SUM_TO_ONE_MAX_POSITION_PCT * equity, exposure_headroom, cash,
                    )
                    if sto_size >= self.broker.min_order_size_usd * 2:
                        try:
                            yes_fill, no_fill = await self.broker.place_sum_to_one_order(sto_opportunity, sto_size)
                            await self.alerter.send_alert(
                                f"[{self.broker.mode}] Sum-to-one {market.asset} ${sto_size:.2f} "
                                f"(YES {yes_fill.avg_price:.3f} + NO {no_fill.avg_price:.3f}, "
                                f"locked edge {sto_opportunity.net_profit_pct:.2%})",
                                level=AlertLevel.INFO,
                            )
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
                fill = await self.broker.place_order(market, signal.side, size_usd)
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

    async def _check_early_exits(self, equity: float) -> None:
        """
        Without this, positions could only ever be held to expiry. Checks
        every open position each cycle for:
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
            market = self._known_markets.get(t["market_id"])
            if market is None or not t["entry_price"]:
                continue

            token_id = market.token_id_yes if t["side"] == "YES" else market.token_id_no
            try:
                book = await self.feed.get_order_book(market.market_id, token_id)
            except Exception:
                continue
            if book.mid is None:
                continue

            unrealized_pct = (book.mid - t["entry_price"]) / t["entry_price"]
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

    def _get_dashboard_state(self) -> DashboardState:
        return self._dashboard_state

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
        ]

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
