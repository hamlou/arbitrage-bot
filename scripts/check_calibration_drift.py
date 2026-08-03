"""
Checks whether the model's predicted probabilities still match realized
outcomes over the last N days of closed paper trades.

For each closed directional paper trade, the model's predicted probability of
winning at entry is recovered from the signal logged for that market just
before the trade opened. (Paper trades store signal_id=None — see
broker_paper.place_order — so the match is by market_id + entry time, not by
foreign key.) That prediction is compared against whether the trade actually
won (realized_pnl_usd > 0).

REPORT ONLY. This script never refits, rewrites, or otherwise touches the live
calibration model (config/calibration.json). Re-fit deliberately with
scripts/calibrate_momentum_model.py when you're ready; this script only tells
you whether recent outcomes have drifted from the predictions.

The per-bin comparison uses the EXACT same binning as
scripts/calibrate_momentum_model.py: engine/calibration.quantile_bin_stats()
— the routine fit_calibration() itself bins with — plus fit_calibration() for
the same-method fitted curve. No separate binning method is invented here.

Usage:
    python scripts/check_calibration_drift.py
    python scripts/check_calibration_drift.py --days 14 --n-bins 8 --tolerance 0.10

Exit codes: 0 = outcomes match predictions (or nothing to check yet);
            1 = drifted beyond tolerance.
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from engine.calibration import fit_calibration, quantile_bin_stats  # noqa: E402
from storage.db import Database  # noqa: E402

# Signals are logged on every evaluation cycle, so a market can have many rows
# close together; the trade opens seconds after the signal that fired it.
MATCH_SLACK_S = 300.0  # how far before the window start we fetch signals


def _latest_signal_before(
    signals_by_market: dict[str, list[dict]], market_id: str, entry_ts: float,
) -> dict | None:
    """Last signal for this market with ts <= entry_ts — the one that fired
    the trade. Lists must be sorted by ts ascending (get_signals is)."""
    by_market = signals_by_market.get(market_id)
    if not by_market:
        return None
    times = [s["ts"] for s in by_market]
    idx = bisect.bisect_right(times, entry_ts) - 1
    if idx < 0:
        return None
    return by_market[idx]


async def check(
    db_path: str,
    *,
    days: float = 7.0,
    n_bins: int = 8,
    tolerance: float = 0.10,
    min_bin_n: int = 10,
    horizon_minutes: int = 15,
) -> int:
    """Returns the process exit code: 0 = match / nothing to check, 1 = drifted."""
    cutoff = time.time() - days * 86400

    db = Database(db_path)
    await db.connect()
    try:
        trades = await db.get_all_trades(mode="PAPER")
        signals = await db.get_signals(since_ts=cutoff - MATCH_SLACK_S)
    finally:
        await db.close()

    closed = [
        t for t in trades
        if t["status"] == "CLOSED"
        and t["realized_pnl_usd"] is not None
        and t["entry_ts"] >= cutoff
    ]
    directional = [t for t in closed if t["strategy"] != "sum_to_one"]
    excluded_sto = len(closed) - len(directional)

    # Index signals by market once — signals are logged every evaluation
    # cycle, so they far outnumber trades; a per-trade linear scan would be
    # O(trades x signals).
    signals_by_market: dict[str, list[dict]] = {}
    for s in signals:
        signals_by_market.setdefault(s["market_id"], []).append(s)

    samples: list[tuple[float, bool]] = []
    unmatched = 0
    for t in directional:
        sig = _latest_signal_before(signals_by_market, t["market_id"], t["entry_ts"])
        if sig is None or sig["implied_prob"] is None:
            unmatched += 1
            continue
        predicted = (
            sig["implied_prob"] if t["side"] == "YES" else 1.0 - sig["implied_prob"]
        )
        samples.append((predicted, t["realized_pnl_usd"] > 0))

    exit_reasons = Counter(t["exit_reason"] for t in directional)

    print("=" * 60)
    print("CALIBRATION DRIFT CHECK")
    print("=" * 60)
    print(f"Window                  : last {days:.1f} days "
          f"(since {datetime.fromtimestamp(cutoff, tz=timezone.utc):%Y-%m-%d %H:%M} UTC)")
    print(f"Closed paper trades     : {len(closed)} ({excluded_sto} sum-to-one excluded)")
    print(f"Directional, signal matched: {len(samples)} ({unmatched} had no logged signal)")
    if exit_reasons:
        print("Exit reasons            : "
              + ", ".join(f"{k} {v}" for k, v in exit_reasons.most_common()))

    if not samples:
        print()
        print("No completed directional paper trades in this window to compare.")
        print("Run the bot in paper mode for a while, then re-run this check.")
        print("=" * 60)
        return 0

    preds = [p for p, _ in samples]
    wins = [1.0 if w else 0.0 for _, w in samples]
    mean_pred = sum(preds) / len(preds)
    win_rate = sum(wins) / len(wins)
    brier = sum((p - w) ** 2 for p, w in samples) / len(samples)

    print()
    print(f"Mean predicted P(win)   : {mean_pred:.3f}")
    print(f"Actual win rate         : {win_rate:.3f}")
    print(f"Overall gap (actual-pred): {win_rate - mean_pred:+.3f}")
    print(f"Brier score             : {brier:.3f}  (0 = perfect, 0.25 = coin flip)")

    # Same binning as scripts/calibrate_momentum_model.py — this is the raw
    # per-bin view (no monotonic enforcement, so drift is not smoothed away).
    breakpoints, rates, counts = quantile_bin_stats(preds, wins, n_bins)
    print()
    print(f"Per-bin calibration (same quantile binning as calibrate_momentum_model.py, "
          f"{n_bins} bins):")
    print(f"  {'bin':>4} {'n':>5} {'mean predicted':>15} {'actual win':>11} {'gap':>8}")
    for i, (bp, rate, n) in enumerate(zip(breakpoints, rates, counts), 1):
        print(f"  {i:>4} {n:>5} {bp:>15.3f} {rate:>11.3f} {rate - bp:>+8.3f}")

    # The same-method fitted curve (monotonic-enforced) — identical shape to
    # what calibrate_momentum_model.py prints for its fitted model.
    model = fit_calibration(samples, horizon_minutes=horizon_minutes, n_bins=n_bins)
    if model is not None:
        print()
        print("Same-method fitted curve (fit_calibration, monotonic-enforced):")
        print(f"  {'breakpoint':>12}  {'P(win)':>10}")
        for bp, prob in zip(model.magnitude_breakpoints, model.continuation_probability):
            print(f"  {bp:12.4f}  {prob:10.3f}")

    # Drift verdict: any bin with enough trades whose actual rate differs from
    # its mean predicted probability by more than tolerance.
    drifted = [
        (bp, rate, n) for bp, rate, n in zip(breakpoints, rates, counts)
        if n >= min_bin_n and abs(rate - bp) > tolerance
    ]
    print("-" * 60)
    if drifted:
        worst = max(drifted, key=lambda x: abs(x[1] - x[0]))
        direction = "overconfident" if worst[1] < worst[0] else "underconfident"
        print(f"RESULT: DRIFTED — {direction} by {abs(worst[1] - worst[0]):.3f} in a bin "
              f"(predicted ~{worst[0]:.3f}, actual {worst[1]:.3f}, n={worst[2]})")
        print("Recent outcomes no longer match the model's probabilities in at least "
              "one bin. Keep paper trading; re-fit only if you've investigated why.")
        rc = 1
    else:
        print(f"RESULT: OUTCOMES STILL MATCH PREDICTIONS "
              f"(no bin with n>={min_bin_n} off by more than {tolerance:.0%})")
        rc = 0
    print("=" * 60)
    print("REPORT ONLY — the live calibration model (config/calibration.json) was "
          "not modified. Re-fit deliberately with scripts/calibrate_momentum_model.py.")
    return rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report whether recent paper-trade outcomes still match the model's "
                    "predicted probabilities, using the same binning as "
                    "scripts/calibrate_momentum_model.py. Never refits the live model."
    )
    parser.add_argument("--days", type=float, default=7.0,
                        help="How many days of closed trades to examine (default 7).")
    parser.add_argument("--n-bins", type=int, default=8,
                        help="Quantile bins for the per-bin comparison (default 8, "
                             "same default as calibrate_momentum_model.py).")
    parser.add_argument("--tolerance", type=float, default=0.10,
                        help="Per-bin |actual - predicted| beyond this counts as drift "
                             "(default 0.10).")
    parser.add_argument("--min-bin-n", type=int, default=10,
                        help="Minimum trades in a bin before its gap counts toward a "
                             "drift verdict (default 10).")
    parser.add_argument("--horizon-minutes", type=int, default=15,
                        help="Label for the fitted curve; pooled across horizons, "
                             "metadata only (default 15).")
    parser.add_argument("--db", type=str, default=settings.DATABASE_PATH,
                        help=f"SQLite database path (default {settings.DATABASE_PATH}).")
    args = parser.parse_args(argv)
    return asyncio.run(check(
        args.db,
        days=args.days,
        n_bins=args.n_bins,
        tolerance=args.tolerance,
        min_bin_n=args.min_bin_n,
        horizon_minutes=args.horizon_minutes,
    ))


if __name__ == "__main__":
    sys.exit(main())
