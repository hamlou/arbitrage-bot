"""
Backtest for the BTC/ETH short-duration UP/DOWN strategy on historical data.

Replays historical Binance price data through the REAL model code
(engine/fair_value.py, engine/signal.py) — nothing here reimplements that
logic. Fills, when order-book data is available, use the REAL mechanics from
engine/broker_paper.py.

Input formats (identical to scripts/calibrate_momentum_model.py — the loader
functions are imported from that module, so they cannot drift apart):
  --price-csv           CSV with columns: timestamp,price (unix seconds, float)
  --binance-klines-csv  raw Binance klines CSV (data.binance.vision 12-column
                        layout, no header; open_time in MILLISECONDS)

Order-book data (optional):
  If a real historical Polymarket order-book snapshot file is available
  (e.g. converted from an archive such as pmxt/Telonex/PolymarketData —
  Step 4.1), pass --orderbook-csv so fills are simulated. Otherwise the
  backtest runs on Binance prices alone and prints an explicit warning that
  slippage and fill quality are NOT modeled. This script NEVER fabricates
  order-book data.

  Order-book CSV format (one row per book level):
    timestamp,market,token,side,price,size
    - timestamp: unix seconds (snapshot time)
    - market:    the window label this snapshot belongs to,
                 e.g. "BTC-15m-1785712000" = {asset}-{duration}m-{contract open time}
    - token:     yes | no
    - side:      bid | ask
    - price:     level price (0-1)
    - size:      level size in shares
  For each contract window the latest snapshot at-or-before the decision
  point is used; windows without a snapshot are skipped (never invented).

How the model is evaluated:
  Each price tick is treated as the open of a 5/15-minute contract.
  reference price = Binance price at contract open (the bot's convention).
  At the decision point (a short lag after open) the model's P(YES) is
  computed with the REAL fair-value model — realized volatility from the
  real RealizedVolatilityEstimator over the real 120s window, then
  fair_value_probability() — and scored against the realized outcome
  (price at the horizon vs the reference).

  NOTE on the simulated timeline: the live engine's confidence formula and
  Market.time_remaining_s read the WALL CLOCK, so each contract window's
  decision tick is anchored to wall-clock "now" (relative spacing between
  ticks preserved). Only the engine's internal clock is anchored; all
  window boundaries and outcomes use the real historical timestamps.

  NOTE on data spacing: the fair-value model needs >= 8 ticks inside its
  120s volatility window. SymbolMomentumTracker keeps only the most recent
  400 ticks, so sub-0.3s-spaced data would silently truncate that window —
  use data spaced >= 0.3s (1s is ideal). 1-minute klines cannot support
  the fair-value path at all.

Trade results are reported both overall and split into THIRDS of the
historical data span (win rate, expectancy net of fees & slippage, and max
drawdown per period) — never just one combined average.

Walk-forward mode (--walk-forward):
  Tests whether the CALIBRATED momentum model (engine/calibration.py) holds
  up on data it never trained on. The dataset is split into N equal-duration
  folds; for each test period, the calibration curve is fit on all price data
  STRICTLY BEFORE that period (expanding window, never touching the test
  period), then every contract window opening inside the period is scored
  with that fitted curve. Repeats forward through the whole dataset. The
  report shows per-fold fit/test spans, pooled out-of-sample Brier/accuracy
  for the calibrated model (plus the parameter-free fair-value model as a
  baseline), an in-sample reference (curve fit on the full dataset), and a
  verdict on whether the fitted edge transfers to unseen data.

  NOTE: the walk-forward feature is the same momentum-over-lookback that
  build_samples_from_price_series() fits on (sign of momentum), so --lookback-s
  is honored by both the fit and the test — this is the calibration curve's
  own feature, not the live engine's 3-tick confirmation gate.

Usage:
    python scripts/backtest.py --price-csv btc_1s.csv --asset BTC --horizon-minutes 15
    python scripts/backtest.py --binance-klines-csv BTCUSDT-1s-2026-06.csv --horizon-minutes 5 \\
        --orderbook-csv polymarket_books.csv
    python scripts/backtest.py --price-csv btc_1s.csv --horizon-minutes 5 --walk-forward \\
        --walk-forward-folds 4 --lookback-s 30 --n-bins 8
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import csv
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import Settings  # noqa: E402
from data.binance_feed import PriceUpdate  # noqa: E402
from data.polymarket_feed import Market, OrderBook, OrderBookLevel  # noqa: E402
from engine.broker_paper import DEFAULT_FEE_PCT, PaperBroker, _round_to_tick  # noqa: E402
from engine.calibration import (  # noqa: E402
    build_samples_from_price_series,
    fit_calibration,
    load_calibration,
)
from engine.fair_value import (  # noqa: E402
    FairValueInputs,
    RealizedVolatilityEstimator,
    fair_value_probability,
)
from engine.signal import (  # noqa: E402
    MIN_TICKS_FOR_VOLATILITY,
    MOMENTUM_LOOKBACK_S,
    SignalEngine,
    SymbolMomentumTracker,
    VOLATILITY_LOOKBACK_S,
)
from scripts.calibrate_momentum_model import load_binance_klines_csv, load_price_csv  # noqa: E402
from storage.db import Database  # noqa: E402

WARNING_NO_ORDERBOOK = (
    "WARNING: no real historical Polymarket order book data was available — "
    "this backtest does not model realistic slippage or fill quality."
)

DEFAULT_DECISION_LAG_S = 15.0
DEFAULT_POSITION_PCT = 0.08  # mirrors Settings.MAX_POSITION_PCT
MODEL_ACCURACY_CUTOFFS = (0.05, 0.10, 0.20, 0.30)
CALIBRATION_BUCKETS = ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0))


@dataclass(frozen=True, slots=True)
class WindowResult:
    open_ts: float
    reference_price: float
    decision_price: float
    p_yes: float
    resolved_yes: bool
    time_remaining_s: float


@dataclass(frozen=True, slots=True)
class TradeResult:
    open_ts: float
    side: str
    size_usd: float
    shares: float
    avg_price: float
    fee_usd: float
    realized_pnl_usd: float
    slippage_pct: float


def _first_index_at_or_after(timestamps: list[float], t: float) -> Optional[int]:
    """First index whose timestamp is >= t, or None if past the end."""
    idx = bisect.bisect_left(timestamps, t)
    return idx if idx < len(timestamps) else None


@dataclass(frozen=True, slots=True)
class _WindowRecord:
    """One evaluated contract window with BOTH model signals: the fair-value
    P(YES) and the momentum feature the calibration curve is fit on."""
    open_ts: float
    reference_price: float
    decision_price: float
    p_yes: Optional[float]        # fair-value model P(YES); None if no volatility estimate
    momentum_pct: Optional[float]  # momentum over lookback at decision point (calibration feature)
    resolved_yes: bool
    time_remaining_s: float


def _replay_windows(
    timestamps: list[float],
    prices: list[float],
    asset: str,
    horizon_s: float,
    decision_lag_s: float,
    lookback_s: float = MOMENTUM_LOOKBACK_S,
) -> list[_WindowRecord]:
    """
    Replays the price series once and evaluates EVERY contract window with the
    REAL engine machinery — engine/signal.py's SymbolMomentumTracker + the
    REAL fair-value model (engine/fair_value.py's RealizedVolatilityEstimator
    and fair_value_probability). No reimplementation. Each record carries both
    the fair-value P(YES) and the momentum-over-lookback feature that
    build_samples_from_price_series()/fit_calibration() train on, so the
    walk-forward mode can score the calibrated model on the SAME windows.
    """
    tracker = SymbolMomentumTracker(lookback_s=lookback_s)
    vol_estimator = RealizedVolatilityEstimator(min_ticks=MIN_TICKS_FOR_VOLATILITY)
    symbol = f"{asset}USDT"
    n = len(timestamps)
    t_last = timestamps[-1]
    results: list[_WindowRecord] = []
    feed_ptr = 0

    for i in range(n):
        t_open = timestamps[i]
        reference = prices[i]
        t_decide = t_open + decision_lag_s
        t_end = t_open + horizon_s
        if t_end > t_last:
            break

        # Re-anchor per window so the decision tick always reads as fresh to
        # the engine's wall-clock logic (see module docstring).
        anchor_now = time.time()
        while feed_ptr < n and timestamps[feed_ptr] <= t_decide:
            tracker.add(
                PriceUpdate(
                    symbol=symbol,
                    price=prices[feed_ptr],
                    event_time_ms=0,
                    received_at=anchor_now - (t_decide - timestamps[feed_ptr]),
                    kind="trade",
                )
            )
            feed_ptr += 1

        # Fair-value signal (may be absent early on — honest None).
        p_yes: Optional[float] = None
        window_prices, window_ts = tracker.prices_and_timestamps(VOLATILITY_LOOKBACK_S)
        sigma = vol_estimator.estimate(window_prices, window_ts)
        if sigma is not None and sigma > 0:
            remaining = t_end - t_decide
            p_yes = fair_value_probability(
                FairValueInputs(
                    current_price=tracker.latest.price,
                    reference_price=reference,
                    time_remaining_s=remaining,
                    volatility_per_sqrt_s=sigma,
                )
            )

        # Calibration feature: momentum over the lookback window, signed — the
        # exact feature build_samples_from_price_series() fits the curve on.
        momentum_pct = tracker.momentum_pct()

        j = _first_index_at_or_after(timestamps, t_end)
        if j is None:
            break
        results.append(
            _WindowRecord(
                open_ts=t_open,
                reference_price=reference,
                decision_price=tracker.latest.price,
                p_yes=p_yes,
                momentum_pct=momentum_pct,
                resolved_yes=prices[j] > reference,
                time_remaining_s=t_end - t_decide,
            )
        )
    return results


def _evaluate_model_windows(
    timestamps: list[float],
    prices: list[float],
    asset: str,
    horizon_s: float,
    decision_lag_s: float,
) -> list[WindowResult]:
    """Fair-value model evaluation over every window (thin projection of the
    shared _replay_windows pass — kept for the non-walk-forward report)."""
    out: list[WindowResult] = []
    for r in _replay_windows(timestamps, prices, asset, horizon_s, decision_lag_s):
        if r.p_yes is None:
            continue  # fair-value path needs a volatility estimate — honest skip
        out.append(
            WindowResult(
                open_ts=r.open_ts,
                reference_price=r.reference_price,
                decision_price=r.decision_price,
                p_yes=r.p_yes,
                resolved_yes=r.resolved_yes,
                time_remaining_s=r.time_remaining_s,
            )
        )
    return out


def load_orderbook_csv(path: Path) -> dict[str, dict[float, dict[str, OrderBook]]]:
    """
    Loads real historical order-book snapshots. Returns
    {market_label: {snapshot_ts: {token: OrderBook}}} — never fabricates data.
    """
    raw: dict[str, dict[float, dict[str, dict[str, list[OrderBookLevel]]]]] = {}
    with path.open() as f:
        for row in csv.DictReader(f):
            market = (row.get("market") or "").strip()
            token = (row.get("token") or "").strip().lower()
            side = (row.get("side") or "").strip().lower()
            if not market or token not in ("yes", "no") or side not in ("bid", "ask"):
                continue
            try:
                ts = float(row["timestamp"])
                price = float(row["price"])
                size = float(row["size"])
            except (TypeError, ValueError):
                continue
            raw.setdefault(market, {}).setdefault(ts, {}).setdefault(token, {}).setdefault(
                side, []
            ).append(OrderBookLevel(price=price, size=size))

    out: dict[str, dict[float, dict[str, OrderBook]]] = {}
    for market, by_ts in raw.items():
        for ts, by_token in by_ts.items():
            per_token: dict[str, OrderBook] = {}
            for token, sides in by_token.items():
                bids = tuple(sorted(sides.get("bid", []), key=lambda lvl: -lvl.price))
                asks = tuple(sorted(sides.get("ask", []), key=lambda lvl: lvl.price))
                per_token[token] = OrderBook(
                    market_id=market, token_id=f"{market}-{token}", bids=bids, asks=asks,
                )
            out.setdefault(market, {})[ts] = per_token
    return out


async def _simulate_trades(
    timestamps: list[float],
    prices: list[float],
    asset: str,
    duration_minutes: int,
    horizon_s: float,
    decision_lag_s: float,
    orderbooks: dict[str, dict[float, dict[str, OrderBook]]],
    settings: Settings,
    *,
    fee_pct: float,
    position_pct: float,
    starting_balance_usd: float,
    min_order_usd: float,
    tick_size: float,
) -> list[TradeResult]:
    """
    Order-book mode: calls the REAL SignalEngine.evaluate() and simulates
    fills with the REAL broker_paper mechanics (PaperBroker._walk_book_for_fill
    + _round_to_tick + the same fee schedule), holding to settlement where a
    winning share pays $1 (same payout as broker_paper.settle_position).
    """
    db = Database(":memory:")  # never connected — evaluate() runs with log=False
    engine = SignalEngine(settings, db=db, calibration=load_calibration())
    fill_broker = PaperBroker(db=db, feed=None, starting_balance_usd=0.0)

    symbol = f"{asset}USDT"
    n = len(timestamps)
    t_last = timestamps[-1]
    balance = starting_balance_usd
    trades: list[TradeResult] = []
    feed_ptr = 0

    for i in range(n):
        t_open = timestamps[i]
        reference = prices[i]
        t_decide = t_open + decision_lag_s
        t_end = t_open + horizon_s
        if t_end > t_last:
            break

        # Re-anchor per window so the decision tick reads as fresh and the
        # simulated time-remaining is exact (see module docstring).
        anchor_now = time.time()

        label = f"{asset}-{duration_minutes}m-{int(t_open)}"
        market_snapshots = orderbooks.get(label)
        if not market_snapshots:
            continue  # no real book data for this window — skipped, never fabricated

        snap_ts: Optional[float] = None
        for ts in sorted(market_snapshots):
            if ts <= t_decide:
                snap_ts = ts
            else:
                break
        if snap_ts is None:
            continue
        token_books = market_snapshots[snap_ts]
        yes_book = token_books.get("yes")
        no_book = token_books.get("no")
        if yes_book is None or no_book is None or yes_book.mid is None:
            continue

        while feed_ptr < n and timestamps[feed_ptr] <= t_decide:
            engine.ingest_price_update(
                PriceUpdate(
                    symbol=symbol,
                    price=prices[feed_ptr],
                    event_time_ms=0,
                    received_at=anchor_now - (t_decide - timestamps[feed_ptr]),
                    kind="trade",
                )
            )
            feed_ptr += 1

        market = Market(
            market_id=label,
            question=f"{asset} Up or Down - {duration_minutes} min",
            token_id_yes=f"{label}-yes",
            token_id_no=f"{label}-no",
            liquidity_usd=0.0,
            end_date_iso="",
            asset=asset,
            duration_minutes=duration_minutes,
            resolved=False,
            reference_price=reference,
            # Anchor expiry to wall-clock now so the REAL engine's
            # time_remaining_s reads the simulated remaining time rather than
            # the (long-past) historical expiry.
            expires_at_ts=anchor_now + (t_end - t_decide),
        )
        signal = await engine.evaluate(market, yes_book, no_book, log=False)
        if not signal.fired:
            continue

        target_book = token_books["yes"] if signal.side == "YES" else token_books["no"]
        if not target_book.asks:
            continue
        size_usd = min(position_pct * balance, balance)
        if size_usd < min_order_usd:
            continue
        try:
            # Real broker_paper fill walk (same function the paper broker uses).
            avg_price, shares = fill_broker._walk_book_for_fill(target_book, size_usd)
        except ValueError:
            continue  # insufficient book depth — same failure mode as the paper broker
        avg_price = _round_to_tick(avg_price, tick_size)
        fee_usd = size_usd * fee_pct
        if size_usd + fee_usd > balance:
            continue
        balance -= size_usd + fee_usd

        j = _first_index_at_or_after(timestamps, t_end)
        resolved_yes = (prices[j] > reference) if j is not None else False
        won = resolved_yes == (signal.side == "YES")
        payout = shares if won else 0.0
        realized = payout - size_usd - fee_usd
        balance += payout

        mid_before = target_book.mid or avg_price
        slippage_pct = (avg_price - mid_before) / mid_before if mid_before else 0.0
        trades.append(
            TradeResult(
                open_ts=t_open,
                side=signal.side,
                size_usd=size_usd,
                shares=shares,
                avg_price=avg_price,
                fee_usd=fee_usd,
                realized_pnl_usd=realized,
                slippage_pct=slippage_pct,
            )
        )
    return trades


def _prediction_stats(probs: list[tuple[float, bool]]) -> dict:
    """
    Accuracy stats over (predicted_probability, resolved_yes) pairs — the
    generic scorer shared by the fair-value report and the walk-forward report
    (calibrated momentum model + fair-value baseline on the same windows).
    """
    n = len(probs)
    if n == 0:
        return {"n": 0}
    brier = sum((p - (1.0 if r else 0.0)) ** 2 for p, r in probs) / n
    mean_abs_dev = sum(abs(p - 0.5) for p, _ in probs) / n
    accuracy = {}
    for c in MODEL_ACCURACY_CUTOFFS:
        sub = [(p, r) for p, r in probs if abs(p - 0.5) >= c]
        if sub:
            correct = sum(1 for p, r in sub if (p > 0.5) == r)
            accuracy[c] = (correct / len(sub), len(sub))
    calibration = []
    for lo, hi in CALIBRATION_BUCKETS:
        sub = [(p, r) for p, r in probs if lo <= p < hi]
        if sub:
            win_rate = sum(1 for _, r in sub if r) / len(sub)
            avg_p = sum(p for p, _ in sub) / len(sub)
            calibration.append((lo, hi, avg_p, win_rate, len(sub)))
    return {"n": n, "brier": brier, "mean_abs_dev": mean_abs_dev,
            "accuracy": accuracy, "calibration": calibration}


def _model_stats(windows: list[WindowResult]) -> dict:
    return _prediction_stats([(w.p_yes, w.resolved_yes) for w in windows])


def _period_metrics(trades: list[TradeResult], starting_balance_usd: float) -> dict:
    """
    Win rate, expectancy per trade, and max drawdown for a subset of trades
    starting from the given balance. Expectancy is net of BOTH fees (deducted
    via fee_usd) and slippage (baked into the share count via the book walk).
    """
    n = len(trades)
    if n == 0:
        return {
            "n": 0, "win_rate": 0.0, "expectancy": 0.0, "total_pnl": 0.0,
            "final_balance": starting_balance_usd, "max_drawdown_pct": 0.0,
            "avg_slippage_pct": 0.0,
        }
    wins = sum(1 for t in trades if t.realized_pnl_usd > 0)
    total_pnl = sum(t.realized_pnl_usd for t in trades)
    running = starting_balance_usd
    peak = running
    max_dd = 0.0
    for t in trades:
        running += t.realized_pnl_usd
        peak = max(peak, running)
        if peak > 0:
            max_dd = max(max_dd, (peak - running) / peak)
    return {
        "n": n,
        "win_rate": wins / n,
        "expectancy": total_pnl / n,
        "total_pnl": total_pnl,
        "final_balance": starting_balance_usd + total_pnl,
        "max_drawdown_pct": max_dd,
        "avg_slippage_pct": sum(t.slippage_pct for t in trades) / n,
    }


def _fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts))


def _print_model_report(windows, asset, horizon_minutes, src_label, n_points, span_days, lag_s) -> None:
    print("=" * 64)
    print(f"BACKTEST REPORT — {asset} {horizon_minutes}-minute UP/DOWN contracts")
    print("=" * 64)
    print(f"Price data     : {src_label} ({n_points} points, {span_days:.1f} days)")
    print(f"Decision lag   : {lag_s:.1f}s after contract open (reference = open price)")
    print()
    if not windows:
        print("No contract windows could be evaluated with the fair-value model.")
        print(
            f"The model needs >= {MIN_TICKS_FOR_VOLATILITY} ticks inside its "
            f"{VOLATILITY_LOOKBACK_S:.0f}s volatility window at the decision point — "
            "use 1-second Binance data and/or a larger --decision-lag-s."
        )
        return
    s = _model_stats(windows)
    print("MODEL EVALUATION (real engine/fair_value.py + engine/signal.py code)")
    print(f"  Windows evaluated    : {s['n']}")
    print(f"  Mean |P(YES) - 0.5|  : {s['mean_abs_dev']:.3f}")
    print(f"  Brier score          : {s['brier']:.3f}  (0 = perfect, 0.25 = coin flip)")
    print("  Accuracy by strength of model lean (P>0.5 predicts YES):")
    for c in MODEL_ACCURACY_CUTOFFS:
        if c in s["accuracy"]:
            acc, cnt = s["accuracy"][c]
            print(f"    |P-0.5| >= {c:.2f} : {acc:.1%} correct (n={cnt})")
    print("  Calibration (model P(YES) vs realized YES rate):")
    for lo, hi, avg_p, win_rate, cnt in s["calibration"]:
        print(f"    P in [{lo:.2f},{hi:.2f}) : avg {avg_p:.3f} -> {win_rate:.1%} realized (n={cnt})")


def _print_trades_report(trades, starting_balance_usd, t_first, t_last) -> None:
    """
    Trade results, split by time period: the historical data span is divided
    into THIRDS and win rate / expectancy / max drawdown are reported per
    period (never just one combined average). Each period's drawdown is
    measured from that period's starting balance (carried forward).
    """
    overall = _period_metrics(trades, starting_balance_usd)
    print()
    print("TRADE SIMULATION (real broker_paper fill mechanics; real order-book snapshots)")
    if overall["n"] == 0:
        print("  No trades — no signal fired on any window with book data (or depth insufficient).")
        return
    print(f"  Trades executed       : {overall['n']}")
    print(f"  Win rate              : {overall['win_rate']:.1%}")
    print(f"  Total PnL (net fees)  : ${overall['total_pnl']:.2f}")
    print(f"  Expectancy / trade    : ${overall['expectancy']:.4f}  (net of fees & slippage)")
    print(f"  Final balance         : ${overall['final_balance']:.2f} (started ${starting_balance_usd:.2f})")
    print(f"  Max drawdown          : {overall['max_drawdown_pct']:.1%}")
    print(f"  Avg slippage vs mid   : {overall['avg_slippage_pct']:.2%}")
    print()
    print("PERFORMANCE BY TIME PERIOD (historical data split into thirds, not a single average):")
    print("  Each period's starting balance carries forward; per-period max drawdown is measured")
    print("  from that period's own starting balance (not the all-time peak).")
    print("  period | trades | win rate | expectancy (net of fees & slippage) | max drawdown")
    span = t_last - t_first
    third = span / 3.0
    carried = starting_balance_usd
    for k in range(3):
        lo = t_first + k * third
        hi = t_first + (k + 1) * third if k < 2 else t_last
        subset = [t for t in trades if lo <= t.open_ts < hi]
        m = _period_metrics(subset, carried)
        label = f"{k + 1}/3 [{_fmt_ts(lo)} .. {_fmt_ts(hi)})"
        if m["n"] == 0:
            print(f"  {label} | no trades")
        else:
            print(
                f"  {label} | {m['n']} trades | win {m['win_rate']:.1%} | "
                f"expectancy ${m['expectancy']:.4f} | max drawdown {m['max_drawdown_pct']:.1%}"
            )
        carried += m["total_pnl"]


def _walk_forward(
    timestamps: list[float],
    prices: list[float],
    asset: str,
    horizon_minutes: int,
    horizon_s: float,
    decision_lag_s: float,
    lookback_s: float,
    n_bins: int,
    folds: int,
) -> dict:
    """
    Walk-forward evaluation of the CALIBRATED momentum model
    (engine/calibration.py) — the trainable part of the strategy.

    The dataset is split into `folds` equal-duration periods. For each test
    period k = 2..folds, the calibration curve is fit on all price data
    STRICTLY BEFORE that period (expanding window, never touching the test
    period), then every contract window opening inside the period is scored
    with that fitted curve — the test period is NEVER re-fit on. The
    parameter-free fair-value model (engine/fair_value.py) is scored on the
    same windows as a baseline, and an in-sample reference (curve fit on the
    full dataset, scored on everything) gives the optimistic ceiling to
    compare against.

    Returns a dict with per-fold results, pooled out-of-sample stats, and
    the in-sample reference — consumed by _print_walk_forward_report().
    """
    records = _replay_windows(
        timestamps, prices, asset, horizon_s, decision_lag_s, lookback_s=lookback_s,
    )
    t0, t_last = timestamps[0], timestamps[-1]
    span = t_last - t0
    fold_dur = span / folds

    def _calibrated_probs(model, recs):
        """Score windows with the calibrated curve: P(YES) from the signed
        momentum feature, using the SAME implied_probability() the live
        engine's fallback calls."""
        out = []
        for r in recs:
            if r.momentum_pct is None or abs(r.momentum_pct) == 0:
                continue  # the fit skips zero-momentum samples; so do we
            direction = "UP" if r.momentum_pct > 0 else "DOWN"
            out.append((model.implied_probability(r.momentum_pct, direction), r.resolved_yes))
        return out

    per_fold = []
    oos_calib: list[tuple[float, bool]] = []
    oos_fv: list[tuple[float, bool]] = []

    for k in range(1, folds):
        test_start = t0 + k * fold_dur
        # No window can open exactly at t_last (replay stops when the horizon
        # runs past the end), so the last fold's end bound is just t_last.
        test_end = t0 + (k + 1) * fold_dur if k < folds - 1 else t_last

        # Training data: every point strictly before the test period.
        train_ts = [ts for ts in timestamps if ts < test_start]
        train_px = [p for p, ts in zip(prices, timestamps) if ts < test_start]
        samples = build_samples_from_price_series(
            train_ts, train_px, lookback_s=lookback_s, horizon_s=horizon_s,
        )
        model = fit_calibration(samples, horizon_minutes=horizon_minutes, n_bins=n_bins)

        test_recs = [r for r in records if test_start <= r.open_ts < test_end]
        calib_probs = _calibrated_probs(model, test_recs) if model is not None else []
        fv_probs = [(r.p_yes, r.resolved_yes) for r in test_recs if r.p_yes is not None]
        oos_calib.extend(calib_probs)
        oos_fv.extend(fv_probs)

        per_fold.append({
            "fold": k,
            "train_span": (train_ts[0], train_ts[-1]) if train_ts else (test_start, test_start),
            "test_span": (test_start, min(test_end, t_last)),
            "train_points": len(train_ts),
            "train_samples": len(samples),
            "model_fit": model is not None,
            "n_test_windows": len(test_recs),
            "calib": _prediction_stats(calib_probs),
            "fv": _prediction_stats(fv_probs),
        })

    # In-sample reference: curve fit on the FULL series, scored on all windows.
    full_samples = build_samples_from_price_series(
        timestamps, prices, lookback_s=lookback_s, horizon_s=horizon_s,
    )
    full_model = fit_calibration(full_samples, horizon_minutes=horizon_minutes, n_bins=n_bins)
    insample_calib = _calibrated_probs(full_model, records) if full_model is not None else []

    return {
        "folds": folds,
        "lookback_s": lookback_s,
        "n_bins": n_bins,
        "per_fold": per_fold,
        "oos_calib": _prediction_stats(oos_calib),
        "oos_fv": _prediction_stats(oos_fv),
        "insample_calib": _prediction_stats(insample_calib),
    }


def _walk_forward_verdict(res: dict) -> str:
    oos = res["oos_calib"]
    ins = res["insample_calib"]
    fv = res["oos_fv"]
    if oos["n"] == 0:
        return ("no out-of-sample calibrated predictions were produced (per-fold training "
                "slices too small to fit the curve) — nothing can be concluded about hold-up.")
    if ins["n"] == 0:
        base = f"out-of-sample calibrated Brier {oos['brier']:.3f} on {oos['n']} windows, but "
        if fv["n"]:
            return base + ("no in-sample reference could be fit — compare against the "
                           f"fair-value baseline (Brier {fv['brier']:.3f}) instead.")
        return base + "no in-sample reference could be fit, and no fair-value baseline exists either."
    gap = oos["brier"] - ins["brier"]
    base = (f"out-of-sample calibrated Brier {oos['brier']:.3f} (n={oos['n']}) vs "
            f"in-sample reference {ins['brier']:.3f} (n={ins['n']})")
    if gap <= 0.01:
        verdict = f"results HOLD UP on data the model never trained on — {base}."
    elif gap <= 0.05:
        verdict = f"results ROUGHLY hold up — mild out-of-sample degradation: {base} (Δ{gap:+.3f})."
    else:
        verdict = f"results do NOT hold up out-of-sample — {base} (Δ{gap:+.3f}): the fitted edge degrades on unseen data."
    if fv["n"]:
        verdict += (f"  For reference, the parameter-free fair-value model scored Brier "
                    f"{fv['brier']:.3f} on the same out-of-sample windows.")
    return verdict


def _print_walk_forward_report(
    res: dict, asset, horizon_minutes, src_label, n_points, span_days, lag_s,
) -> None:
    print("=" * 64)
    print(f"WALK-FORWARD BACKTEST — {asset} {horizon_minutes}-minute UP/DOWN contracts")
    print("=" * 64)
    print(f"Price data     : {src_label} ({n_points} points, {span_days:.1f} days)")
    print(f"Decision lag   : {lag_s:.1f}s after contract open (reference = open price)")
    print(f"Walk-forward   : {res['folds']} equal-duration folds; each test period's calibration")
    print(f"                 is fit ONLY on price data strictly before it, then applied to the")
    print(f"                 next period without re-fitting (lookback={res['lookback_s']:.0f}s, "
          f"{res['n_bins']} bins)")
    print()
    print("PER-FOLD (fit-before-test, out-of-sample):")
    print(f"  {'fold':>4} | {'train span':>21} | {'test span':>21} | {'trn pts':>7} | "
          f"{'fit?':>4} | {'test':>4} | {'calib Brier':>11} | {'calib acc@.10':>13} | {'fv Brier':>9}")
    for f in res["per_fold"]:
        fit = "yes" if f["model_fit"] else "NO"
        cb, fb = f["calib"], f["fv"]
        calib_b = f"{cb['brier']:.3f}" if cb["n"] else "  -  "
        acc = "  -  "
        if cb["n"] and 0.10 in cb["accuracy"]:
            a, cnt = cb["accuracy"][0.10]
            acc = f"{a:.1%} (n={cnt})"
        fv_b = f"{fb['brier']:.3f}" if fb["n"] else "  -  "
        print(
            f"  {f['fold']:>4} | {_fmt_ts(f['train_span'][0])}..{_fmt_ts(f['train_span'][1])[11:]} | "
            f"{_fmt_ts(f['test_span'][0])}..{_fmt_ts(f['test_span'][1])[11:]} | {f['train_points']:>7} | "
            f"{fit:>4} | {f['n_test_windows']:>4} | {calib_b:>11} | {acc:>13} | {fv_b:>9}"
        )
    print()
    oos, fv, ins = res["oos_calib"], res["oos_fv"], res["insample_calib"]
    print("POOLED OUT-OF-SAMPLE (all tested folds — data the model never trained on):")
    if oos["n"]:
        print(f"  Calibrated momentum model : n={oos['n']}  Brier={oos['brier']:.3f}  "
              f"mean|P-0.5|={oos['mean_abs_dev']:.3f}")
        print("    accuracy by lean strength:")
        for c in MODEL_ACCURACY_CUTOFFS:
            if c in oos["accuracy"]:
                acc, cnt = oos["accuracy"][c]
                print(f"      |P-0.5| >= {c:.2f} : {acc:.1%} correct (n={cnt})")
    else:
        print("  Calibrated momentum model : no predictions (fits skipped).")
    if fv["n"]:
        print(f"  Fair-value model baseline : n={fv['n']}  Brier={fv['brier']:.3f}")
    print()
    print("IN-SAMPLE REFERENCE (calibration fit on the FULL dataset, scored on the same data):")
    if ins["n"]:
        print(f"  Calibrated momentum model : n={ins['n']}  Brier={ins['brier']:.3f}  "
              f"mean|P-0.5|={ins['mean_abs_dev']:.3f}")
    else:
        print("  (could not fit a curve on the full series)")
    print()
    print("VERDICT:", _walk_forward_verdict(res))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backtest the BTC/ETH short-duration strategy on historical Binance "
            "price data, using the real engine model code (and optional real "
            "Polymarket order-book snapshots for fill simulation)."
        ),
    )
    parser.add_argument("--price-csv", type=str, default="",
                        help="CSV with columns: timestamp,price (same format as scripts/calibrate_momentum_model.py)")
    parser.add_argument("--binance-klines-csv", type=str, default="",
                        help="Raw Binance klines CSV (data.binance.vision format, same as calibrate script)")
    parser.add_argument("--orderbook-csv", type=str, default="",
                        help="Optional real historical Polymarket order-book snapshots (see module docstring for format)")
    parser.add_argument("--asset", type=str, default="BTC", choices=["BTC", "ETH"])
    parser.add_argument("--horizon-minutes", type=int, default=15, choices=[5, 15])
    parser.add_argument("--decision-lag-s", type=float, default=DEFAULT_DECISION_LAG_S)
    parser.add_argument("--fee-pct", type=float, default=DEFAULT_FEE_PCT)
    parser.add_argument("--position-pct", type=float, default=DEFAULT_POSITION_PCT)
    parser.add_argument("--starting-balance-usd", type=float, default=1000.0)
    parser.add_argument("--min-order-usd", type=float, default=1.0)
    parser.add_argument("--tick-size", type=float, default=0.01)
    parser.add_argument("--edge-threshold-pct", type=float, default=0.05)
    parser.add_argument("--min-confidence", type=float, default=0.85)
    parser.add_argument("--min-liquidity-usd", type=float, default=50_000.0)
    parser.add_argument("--walk-forward", action="store_true",
                        help="Walk-forward evaluation: fit calibration on data strictly before "
                             "each test period, test on the next period WITHOUT re-fitting, "
                             "repeat forward — reports whether results hold up out-of-sample")
    parser.add_argument("--walk-forward-folds", type=int, default=4,
                        help="Number of equal-duration folds for --walk-forward (default 4; "
                             "the first period is training-only, periods 2..N are tested)")
    parser.add_argument("--lookback-s", type=float, default=MOMENTUM_LOOKBACK_S,
                        help=f"Momentum lookback in seconds used for the calibration feature "
                             f"(default matches engine/signal.py: {MOMENTUM_LOOKBACK_S:.0f}s)")
    parser.add_argument("--n-bins", type=int, default=8,
                        help="Calibration bins per fold fit (default 8)")
    args = parser.parse_args()

    if not args.price_csv and not args.binance_klines_csv:
        print("Provide either --price-csv or --binance-klines-csv (same formats as "
              "scripts/calibrate_momentum_model.py).")
        return 1

    if args.binance_klines_csv:
        timestamps, prices = load_binance_klines_csv(Path(args.binance_klines_csv))
        src_label = f"binance klines {args.binance_klines_csv}"
    else:
        timestamps, prices = load_price_csv(Path(args.price_csv))
        src_label = f"price csv {args.price_csv}"

    if len(timestamps) < 2:
        print(f"Only {len(timestamps)} price points loaded — need at least 2.")
        return 1
    if any(b <= a for a, b in zip(timestamps, timestamps[1:])):
        print("Price data timestamps are not strictly ascending — cannot backtest.")
        return 1

    horizon_s = args.horizon_minutes * 60
    if args.decision_lag_s >= horizon_s:
        print(f"--decision-lag-s ({args.decision_lag_s:.0f}s) must be smaller than "
              f"the horizon ({horizon_s}s).")
        return 1

    has_orderbook = bool(args.orderbook_csv)
    # The warning (when no real order-book data is available) is deliberately
    # the very first line of every report.
    if has_orderbook:
        orderbooks = load_orderbook_csv(Path(args.orderbook_csv))
        snapshot_count = sum(len(v) for v in orderbooks.values())
        print(f"Using real order-book snapshots: {args.orderbook_csv} ({snapshot_count} snapshots)")
    else:
        print(WARNING_NO_ORDERBOOK)
    print()

    if args.walk_forward:
        if args.walk_forward_folds < 2:
            print("--walk-forward-folds must be at least 2 (period 1 is training-only).")
            return 1
        wf = _walk_forward(
            timestamps, prices, args.asset, args.horizon_minutes, horizon_s,
            args.decision_lag_s, args.lookback_s, args.n_bins, args.walk_forward_folds,
        )
        _print_walk_forward_report(
            wf, args.asset, args.horizon_minutes, src_label, len(prices),
            (timestamps[-1] - timestamps[0]) / 86400, args.decision_lag_s,
        )
    else:
        windows = _evaluate_model_windows(
            timestamps, prices, args.asset, horizon_s, args.decision_lag_s,
        )
        _print_model_report(
            windows, args.asset, args.horizon_minutes, src_label, len(prices),
            (timestamps[-1] - timestamps[0]) / 86400, args.decision_lag_s,
        )

    if has_orderbook:
        if args.walk_forward:
            print("NOTE: trade simulation below uses the repo's SAVED calibration "
                  "(config/calibration.json), not the per-fold walk-forward fits — "
                  "it tests fill mechanics, not the fitted curve.")
        settings = Settings(
            _env_file=None,
            EDGE_THRESHOLD_PCT=args.edge_threshold_pct,
            MIN_CONFIDENCE=args.min_confidence,
            MIN_MARKET_LIQUIDITY_USD=args.min_liquidity_usd,
            DATABASE_PATH="storage/backtest.db",
        )
        trades = asyncio.run(
            _simulate_trades(
                timestamps, prices, args.asset, args.horizon_minutes, horizon_s,
                args.decision_lag_s, orderbooks, settings,
                fee_pct=args.fee_pct,
                position_pct=args.position_pct,
                starting_balance_usd=args.starting_balance_usd,
                min_order_usd=args.min_order_usd,
                tick_size=args.tick_size,
            )
        )
        _print_trades_report(trades, args.starting_balance_usd, timestamps[0], timestamps[-1])

    return 0


if __name__ == "__main__":
    sys.exit(main())
