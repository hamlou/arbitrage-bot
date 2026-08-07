"""
Divergence-detection signal engine. This module only produces signals — it
does not size or execute anything (that's engine/risk.py and the broker).

Two probability models are available, in priority order:

1. Fair value (engine/fair_value.py) — PRIMARY. Directly answers what the
   contract actually resolves on: given the current price relative to the
   market's REFERENCE price, remaining time, and recent volatility, what's
   the probability of finishing above reference? This replaces an earlier
   design that compared recent momentum to the market's CURRENT price
   without ever considering the reference price at all — which could fire
   an "UP" signal on upward momentum even while price sat well below the
   level the contract actually needs to beat. Requires a reference_price
   and enough recent ticks to estimate volatility; when either is missing
   (e.g. right after startup, or for a market whose reference price wasn't
   captured), this falls back to:

2. Momentum heuristic/calibration (unchanged from the original design) —
   compares recent momentum to Polymarket's current price. Cruder, but
   available immediately with no reference-price dependency.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

from config.settings import Settings
from data.binance_feed import PriceUpdate
from data.polymarket_feed import Market, OrderBook
from engine.calibration import CalibrationModel
from engine.fair_value import FairValueInputs, RealizedVolatilityEstimator, fair_value_probability
from engine.fees import round_trip_fee_pct
from storage.db import Database

logger = logging.getLogger(__name__)

CONFIRMATION_WINDOW = 3        # consecutive ticks required to agree on direction (momentum fallback only)
MOMENTUM_LOOKBACK_S = 30.0     # window over which we measure recent price change (momentum fallback only)
VOLATILITY_LOOKBACK_S = 120.0  # wider window for a more stable realized-vol estimate (fair value model)
MIN_TICKS_FOR_VOLATILITY = 8
# A price tick older than this (seconds) is treated as "missing" by the
# cross-exchange gate rather than as a real disagreement — a silently-stalled
# feed (connection alive, but no trades) must never lock the bot out of
# trading; that would turn a sanity check into an accidental kill switch.
CROSS_EXCHANGE_MAX_AGE_S = 10.0


@dataclass(frozen=True, slots=True)
class Signal:
    market: Market
    side: str              # "YES" or "NO" — the side implied to be underpriced
    implied_prob: float
    polymarket_prob: float
    edge_pct: float
    confidence: float
    fired: bool
    reason: str
    model_used: str = ""   # "fair_value" or "momentum_fallback", for auditability


class SymbolMomentumTracker:
    """
    Rolling window of recent Binance ticks for one symbol (e.g. BTCUSDT).
    Serves both probability models: direction/momentum for the fallback
    heuristic, and the raw (price, timestamp) series for realized-volatility
    estimation feeding the fair value model.
    """

    def __init__(self, lookback_s: float = MOMENTUM_LOOKBACK_S, maxlen: int = 400):
        self.lookback_s = lookback_s
        self._ticks: Deque[PriceUpdate] = deque(maxlen=maxlen)

    def add(self, update: PriceUpdate) -> None:
        self._ticks.append(update)

    @property
    def latest(self) -> Optional[PriceUpdate]:
        return self._ticks[-1] if self._ticks else None

    def _ticks_in_window(self, lookback_s: Optional[float] = None) -> list[PriceUpdate]:
        if not self._ticks:
            return []
        window = lookback_s if lookback_s is not None else self.lookback_s
        cutoff = self._ticks[-1].received_at - window
        return [t for t in self._ticks if t.received_at >= cutoff]

    def momentum_pct(self) -> Optional[float]:
        """Percent price change over the lookback window. None if insufficient data."""
        window = self._ticks_in_window()
        if len(window) < 2:
            return None
        start, end = window[0].price, window[-1].price
        if start == 0:
            return None
        return (end - start) / start

    def direction_confirmed(self, n: int = CONFIRMATION_WINDOW) -> Optional[str]:
        """
        Returns "UP", "DOWN", or None. Requires the last `n` consecutive ticks
        to agree on direction — guards against firing on a single noisy tick.
        """
        if len(self._ticks) < n:
            return None
        recent = list(self._ticks)[-n:]
        diffs = [b.price - a.price for a, b in zip(recent, recent[1:])]
        if all(d > 0 for d in diffs):
            return "UP"
        if all(d < 0 for d in diffs):
            return "DOWN"
        return None

    def tick_age_s(self) -> Optional[float]:
        return self.latest.age_seconds if self.latest else None

    def prices_and_timestamps(self, lookback_s: float) -> tuple[list[float], list[float]]:
        window = self._ticks_in_window(lookback_s)
        return [t.price for t in window], [t.received_at for t in window]


def _implied_prob_from_momentum(momentum_pct: float, direction: str, sensitivity: float = 8.0) -> float:
    """
    Rough mapping from recent momentum magnitude to an implied probability
    that the asset is higher (YES) at contract expiry. Intentionally simple
    and monotonic — this is the FALLBACK model, used only when the fair
    value model's inputs (reference price, volatility estimate) aren't yet
    available. Note this deliberately does NOT account for the contract's
    reference price, which is exactly the limitation the fair value model
    exists to fix; don't rely on this path once fair value is available.
    """
    magnitude = min(abs(momentum_pct) * sensitivity, 0.49)
    base = 0.5 + magnitude if direction == "UP" else 0.5 - magnitude
    return max(0.01, min(0.99, base))


def _confidence_score(
    tick_age_s: Optional[float],
    book_depth_usd: float,
    direction_confirmed: bool,
    min_liquidity_usd: float,
) -> float:
    """
    Confidence in [0, 1], built from three independent factors:
      - data freshness (Binance tick age)
      - order book depth on the ACTUAL target side (see evaluate() — this now
        always receives the correct side's book, not always the YES book)
      - directional consistency
    """
    if tick_age_s is None:
        freshness_score = 0.0
    else:
        # Full marks under 1s old, linearly decaying to 0 by 5s old.
        freshness_score = max(0.0, min(1.0, 1.0 - (tick_age_s - 1.0) / 4.0))

    depth_score = max(0.0, min(1.0, book_depth_usd / (min_liquidity_usd * 2)))
    consistency_score = 1.0 if direction_confirmed else 0.0

    return (freshness_score + depth_score + consistency_score) / 3.0


class SignalEngine:
    def __init__(self, settings: Settings, db: Database, calibration: Optional[dict[int, CalibrationModel]] = None):
        self.settings = settings
        self.db = db
        self._trackers: dict[str, SymbolMomentumTracker] = {}
        self._calibration = calibration or {}
        self._vol_estimator = RealizedVolatilityEstimator(min_ticks=MIN_TICKS_FOR_VOLATILITY)
        # Latest price + tick age per (source, symbol) — the cross-exchange
        # sanity gate compares Binance vs Coinbase here before firing a
        # signal. Timestamps are kept so a STALE tick (feed alive but silent)
        # is treated as missing, never as a real disagreement.
        self._latest_price: dict[tuple[str, str], float] = {}
        self._latest_received_at: dict[tuple[str, str], float] = {}

    def _tracker_for(self, symbol: str) -> SymbolMomentumTracker:
        if symbol not in self._trackers:
            self._trackers[symbol] = SymbolMomentumTracker()
        return self._trackers[symbol]

    def current_price(self, asset: str) -> Optional[float]:
        """Latest known Binance price for an asset (e.g. "BTC") — used by
        main.py's discovery loop to capture a new market's reference price
        at first sighting."""
        tracker = self._trackers.get(f"{asset}USDT")
        return tracker.latest.price if tracker and tracker.latest else None

    def latest_tick_received_at(self, asset: str) -> Optional[float]:
        """Wall-clock time the most recent Binance tick for an asset was
        received, or None if none has arrived yet. main.py uses this as the
        latency cycle's true start time — without it, measured "tick->order"
        latency only covered the time inside one poll cycle and hid the up-to-
        1s poll wait that actually decides whether the arbitrage window is
        winnable."""
        tracker = self._trackers.get(f"{asset}USDT")
        return tracker.latest.received_at if tracker and tracker.latest else None

    def _momentum_implied_probability(self, momentum: float, direction: str, duration_minutes: int) -> float:
        model = self._calibration.get(duration_minutes)
        if model is not None:
            return model.implied_probability(momentum, direction)
        return _implied_prob_from_momentum(momentum, direction)

    def ingest_price_update(self, update: PriceUpdate, source: str = "binance") -> None:
        """
        Feed a normalized price tick into the engine.

        `source` tags which exchange the tick came from ("binance" or
        "coinbase"). Binance ticks feed the momentum tracker the models run
        on (the single consistent price series); Coinbase ticks are recorded
        only for the cross-exchange sanity gate in evaluate() so they don't
        pollute the model's momentum/volatility inputs with a second source.
        The default stays "binance" so existing callers are unchanged.
        """
        self._latest_price[(source, update.symbol)] = update.price
        self._latest_received_at[(source, update.symbol)] = update.received_at
        if source == "coinbase":
            return  # gate-only source — never feed the model tracker
        self._tracker_for(update.symbol).add(update)

    def _fresh_latest_price(self, source: str, symbol: str) -> Optional[float]:
        """Latest price for (source, symbol) if it arrived within
        CROSS_EXCHANGE_MAX_AGE_S — otherwise None (treated as "can't judge"
        by the gate, never as a real disagreement)."""
        price = self._latest_price.get((source, symbol))
        received_at = self._latest_received_at.get((source, symbol))
        if price is None or received_at is None:
            return None
        if time.time() - received_at > CROSS_EXCHANGE_MAX_AGE_S:
            return None
        return price

    def cross_exchange_disagreement_pct(self, symbol: str) -> Optional[float]:
        """
        Percent difference between the latest FRESH Binance and Coinbase prices
        for a symbol, or None if either source has no recent tick (or its
        price is unusable). A None means "can't judge" — the gate in
        evaluate() treats that as no disagreement rather than blocking.
        """
        binance_px = self._fresh_latest_price("binance", symbol)
        coinbase_px = self._fresh_latest_price("coinbase", symbol)
        if binance_px is None or coinbase_px is None or binance_px == 0:
            return None
        return abs(coinbase_px - binance_px) / binance_px * 100.0

    async def evaluate(
        self, market: Market, yes_book: OrderBook, no_book: OrderBook, log: bool = True,
    ) -> Signal:
        """
        Compare a model-implied probability of YES against Polymarket's live
        order-book-derived probability, and decide whether to fire a signal.

        Takes BOTH the YES and NO token books — the earlier version only ever
        fetched the YES book, which meant confidence/depth scoring for a
        NO-side signal used the wrong book as an approximation. Every
        evaluation is logged to SQLite by default (log=False lets callers
        recompute a fresh reading for exit-check purposes without polluting
        the audit trail with extra rows).
        """
        symbol = f"{market.asset}USDT"
        tracker = self._tracker_for(symbol)
        tick_age = tracker.tick_age_s()
        current_price = tracker.latest.price if tracker.latest else None

        # -- Cross-exchange sanity gate: computed FIRST, because it's a pure
        # price comparison that doesn't depend on model readiness. The audit
        # row must record EVERY above-threshold disagreement — even during
        # insufficient-data windows (no fair-value inputs / no confirmed
        # momentum) when no signal could have fired at all, and even from
        # log=False recomputes (which are the ONLY evaluate calls markets
        # with an open position get, from _check_early_exits). Gating the
        # audit row on `log` would silently drop held-market observations.
        disagreement_pct = self.cross_exchange_disagreement_pct(symbol)
        cross_exchange_blocked = (
            disagreement_pct is not None
            and disagreement_pct > self.settings.CROSS_EXCHANGE_TOLERANCE_PCT
        )
        if cross_exchange_blocked:
            binance_px = self._fresh_latest_price("binance", symbol)
            coinbase_px = self._fresh_latest_price("coinbase", symbol)
            logger.info(
                "Skipping signal for %s: cross-exchange price disagreement %.4f%% "
                "> tolerance %.4f%% (binance=%s, coinbase=%s) — reason=cross_exchange_disagreement",
                symbol, disagreement_pct, self.settings.CROSS_EXCHANGE_TOLERANCE_PCT,
                binance_px, coinbase_px,
            )
            # Record the observation regardless of whether it actually
            # suppressed a would-be signal (the gate blocks on all of them,
            # but the audit row must exist even when no signal was otherwise
            # about to fire) — that's what makes disagreement-frequency
            # review possible later. Note: one row per market evaluation
            # while disagreeing (up to ~1/sec per market during a sustained
            # divergence) — that volume is the point of the audit, but don't
            # read it as a count of distinct events. Written even on log=False
            # recomputes (held markets are only ever evaluated that way);
            # skipped when the DB isn't connected (scripts like backtest.py
            # run evaluate() against an unconnected in-memory Database).
            if (
                binance_px is not None
                and coinbase_px is not None
                and self.db.connected
            ):
                try:
                    await self.db.log_exchange_disagreement(
                        symbol=symbol,
                        binance_price=binance_px,
                        coinbase_price=coinbase_px,
                        disagreement_pct=disagreement_pct,
                    )
                except Exception:
                    # An observational-log write must never break the trading
                    # cycle — the disagreement was already logged above.
                    logger.exception("Failed to record exchange disagreement for %s", symbol)
        elif disagreement_pct is None:
            # Gate can't judge (one feed down or stale) — visible at debug so
            # operators know the sanity check is currently inactive.
            logger.debug(
                "Cross-exchange gate inactive for %s (missing or stale source price)", symbol
            )

        polymarket_prob = yes_book.mid

        implied_prob: Optional[float] = None
        model_used = ""
        direction_ok = False  # used only for the confidence score's consistency factor

        # -- Primary: fair value model, if we have what it needs --
        if market.reference_price is not None and current_price is not None:
            time_remaining_s = market.time_remaining_s
            if time_remaining_s is not None and time_remaining_s > 0:
                prices, timestamps = tracker.prices_and_timestamps(VOLATILITY_LOOKBACK_S)
                sigma = self._vol_estimator.estimate(prices, timestamps)
                if sigma is not None and sigma > 0:
                    implied_prob = fair_value_probability(FairValueInputs(
                        current_price=current_price,
                        reference_price=market.reference_price,
                        time_remaining_s=time_remaining_s,
                        volatility_per_sqrt_s=sigma,
                    ))
                    model_used = "fair_value"
                    direction_ok = True  # the z-score itself already accounts for noise; no separate gate needed

        # -- Fallback: momentum heuristic/calibration --
        if implied_prob is None:
            direction = tracker.direction_confirmed()
            momentum = tracker.momentum_pct()
            if direction is not None and momentum is not None:
                implied_prob = self._momentum_implied_probability(momentum, direction, market.duration_minutes)
                model_used = "momentum_fallback"
                direction_ok = True

        if implied_prob is None or polymarket_prob is None:
            reason = "insufficient data (no fair-value inputs and no confirmed momentum yet)"
            sig = Signal(
                market=market, side="", implied_prob=0.0, polymarket_prob=polymarket_prob or 0.0,
                edge_pct=0.0, confidence=0.0, fired=False, reason=reason, model_used=model_used,
            )
            if log:
                await self._log(sig, tick_age)
            return sig

        # Clamp the model's implied probability into a sane band. Verified
        # 2026-08-06: the fair-value model routinely saturated to ~0.0 / ~1.0
        # (implied=0.99999998 against a market reading 0.085), and the bot
        # bought YES @ 0.82 / NO @ 0.99 on those overconfident reads, losing
        # ~$170 on two trades. A model that says "99.999998%" is not a signal
        # — it's a degenerate input, and an edge computed from it is fiction.
        #
        # A read that HITS the clamp boundary is by definition untrustworthy
        # (the clamp exists because these values are degenerate), so such a
        # signal must NOT fire even after clamping — clamping alone shrinks
        # the edge but 0.98 - 0.45 = 0.53 would still clear the threshold and
        # trade a degenerate read at a rich price (the real YES @ 0.45,
        # -$88.43 loss). If the raw value was outside the band, the model is
        # saturated and there is no reliable edge to act on.
        IMPLIED_PROB_MIN, IMPLIED_PROB_MAX = 0.02, 0.98
        model_saturated = implied_prob < IMPLIED_PROB_MIN or implied_prob > IMPLIED_PROB_MAX
        implied_prob = max(IMPLIED_PROB_MIN, min(IMPLIED_PROB_MAX, implied_prob))

        edge_pct = abs(implied_prob - polymarket_prob)
        target_side = "YES" if implied_prob > polymarket_prob else "NO"
        target_book = yes_book if target_side == "YES" else no_book
        # We always BUY the target side's token, so depth on that token's own
        # ask side is what matters — not a proxy inferred from the other book.
        depth_usd = target_book.depth_usd("ask")

        # -- Fresh-move gate: only trade a REAL lag, never a drift --
        # The strategy's premise is "Polymarket LAGS a fresh Binance move."
        # The model's direction must therefore agree with the asset's ACTUAL
        # recent movement. Verified 2026-08-07: the model bought NO @ 0.69
        # (implied NO 0.86) while BTC was rising and the market held YES at
        # 0.30-0.33 — a 25s drift from a stale reference price, not a lag.
        # The market repriced against us and the position hit ~$0 in 5s.
        # Requiring recent aligned momentum blocks exactly that failure: no
        # fresh move in the model's direction -> there is nothing for the
        # market to lag -> the "edge" is model error, not mispricing.
        #
        # Deliberate asymmetry: the momentum FALLBACK derives its probability
        # from a 30s window (MOMENTUM_LOOKBACK_S) while this gate uses the 15s
        # FRESH_MOVE_LOOKBACK_S. A move that happened ~20s ago therefore has
        # strong 30s momentum but weak 15s momentum and gets blocked here.
        # That is intentional: Polymarket usually reprices a dislocation
        # within seconds, so a 20s-old move is already priced — nothing left
        # to front-run.
        fresh_move_ok = True
        fresh_move_reason = ""
        prices, _ = tracker.prices_and_timestamps(self.settings.FRESH_MOVE_LOOKBACK_S)
        if len(prices) >= 2 and prices[0] > 0:
            move = (prices[-1] - prices[0]) / prices[0]
            aligned = (
                (implied_prob > polymarket_prob and move > self.settings.FRESH_MOVE_MIN_PCT)
                or (implied_prob < polymarket_prob and move < -self.settings.FRESH_MOVE_MIN_PCT)
            )
            if not aligned:
                fresh_move_ok = False
                fresh_move_reason = (
                    f"no fresh aligned move (last {self.settings.FRESH_MOVE_LOOKBACK_S:.0f}s "
                    f"momentum {move * 100:.3f}% vs required "
                    f"±{self.settings.FRESH_MOVE_MIN_PCT * 100:.2f}% in model direction)"
                )
        else:
            fresh_move_ok = False
            fresh_move_reason = "insufficient ticks to confirm a fresh move"
        if (
            market.time_remaining_s is not None
            and market.time_remaining_s < self.settings.MIN_ENTRY_TIME_REMAINING_S
        ):
            fresh_move_ok = False
            fresh_move_reason = (
                f"only {market.time_remaining_s:.0f}s left — below the "
                f"{self.settings.MIN_ENTRY_TIME_REMAINING_S:.0f}s minimum for entries"
            )

        confidence = _confidence_score(
            tick_age_s=tick_age,
            book_depth_usd=depth_usd,
            direction_confirmed=direction_ok,
            min_liquidity_usd=self.settings.MIN_MARKET_LIQUIDITY_USD,
        )

        # The gate result computed above is applied here: if Binance and
        # Coinbase disagree beyond tolerance, the model read is untrustworthy
        # — force fired=False and zero side/edge so downstream consumers
        # (e.g. _check_early_exits' EDGE_REVERSAL path, which does NOT check
        # `fired`) can't act on a gated reading.
        if cross_exchange_blocked:
            fired = False
            reason = "cross_exchange_disagreement"
            # The model read is untrustworthy while exchanges disagree — zero
            # the side/edge so downstream consumers (e.g. _check_early_exits'
            # EDGE_REVERSAL path, which does NOT check `fired`) can't act on it.
            target_side = ""
            edge_pct = 0.0
        else:
            # Fee-aware edge (price-dependent, per docs.polymarket.com/trading/
            # fees): Polymarket's crypto taker fee is fee_rate * p * (1-p) per
            # share — ~3.5% of notional per side at p=0.50, so ~7% for a taker
            # round trip. The raw model-vs-market gap must clear that or the
            # "edge" is consumed by fees (settlement is free — no exit fee at
            # resolution). The flat-2% assumption this replaced UNDERSTATED
            # mid-price fees, making paper results look better than live.
            entry_price = target_book.best_ask if target_book.best_ask is not None else polymarket_prob
            fee = round_trip_fee_pct(entry_price, fee_rate=self.settings.TAKER_FEE_PCT)
            net_edge = edge_pct - fee
            # Price sanity: buying a token above MAX_DIRECTIONAL_ENTRY_PRICE
            # means break-even requires being right 80%+ of the time — no
            # model on a 5-minute window deserves that. Reject rather than
            # overpay.
            entry_price_ok = True
            if target_book.best_ask is not None and target_book.best_ask > self.settings.MAX_DIRECTIONAL_ENTRY_PRICE:
                entry_price_ok = False
                reason = (
                    f"entry ask {target_book.best_ask:.3f} > max "
                    f"{self.settings.MAX_DIRECTIONAL_ENTRY_PRICE:.2f} (break-even "
                    "probability too high after fees)"
                )
            elif model_saturated:
                # A degenerate read is more fundamental than any missing fresh
                # move — report the saturation, not the momentum.
                reason = "model read saturated (clamped to sane band) — degenerate, not tradable"
            elif not fresh_move_ok:
                reason = fresh_move_reason
            else:
                reason = ""

            fired = (
                net_edge > self.settings.EDGE_THRESHOLD_PCT
                and confidence > self.settings.MIN_CONFIDENCE
                and entry_price_ok
                and not model_saturated
                and fresh_move_ok
            )
            if not reason:
                reason = "OK" if fired else (
                    f"net edge {net_edge:.3f} <= threshold {self.settings.EDGE_THRESHOLD_PCT:.3f}"
                    if net_edge <= self.settings.EDGE_THRESHOLD_PCT
                    else f"confidence {confidence:.3f} <= min {self.settings.MIN_CONFIDENCE:.3f}"
                )

        sig = Signal(
            market=market, side=target_side, implied_prob=implied_prob,
            polymarket_prob=polymarket_prob, edge_pct=edge_pct, confidence=confidence,
            fired=fired, reason=reason, model_used=model_used,
        )
        if log:
            await self._log(sig, tick_age, depth_usd)
        return sig

    async def _log(self, sig: Signal, tick_age: Optional[float], depth_usd: Optional[float] = None) -> None:
        await self.db.log_signal(
            market_id=sig.market.market_id,
            asset=sig.market.asset,
            implied_prob=sig.implied_prob,
            polymarket_prob=sig.polymarket_prob,
            edge_pct=sig.edge_pct,
            confidence=sig.confidence,
            fired=sig.fired,
            reason=f"[{sig.model_used}] {sig.reason}" if sig.model_used else sig.reason,
            binance_tick_age_s=tick_age,
            book_depth_usd=depth_usd,
        )
