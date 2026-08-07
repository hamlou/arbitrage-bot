"""
Breakdown report for the validation run — the pieces validate_paper_run.py
doesn't produce:

  (a) win rate and net PnL split by ENTRY PATH:
        - latency_arb      = 1s polling-cycle entries
        - latency_arb_fast = event-driven fast-path entries (label added
                             2026-08-07 — see main.py)
        - sum_to_one       = combo arb (risk-free, no directional forecast)
  (b) count of signals BLOCKED by each gate (fired=0), classified from the
      audited `reason` string in the signals table — so we can see whether
      the new gates actually improved quality instead of assuming it.

Also prints an overall snapshot (trades, win rate, net PnL, profit factor)
and the latency percentiles from latency_events.

Usage:
    python scripts/analyze_run.py
    python scripts/analyze_run.py --since-ts 1780000000
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from storage.db import Database  # noqa: E402

# Gate classifier: signals with fired=0 are blocked; their `reason` tells us
# which gate did it. The engine prefixes reasons with "[<model>] " — strip it.
GATES = [
    ("cross-exchange", "cross_exchange_disagreement"),
    ("saturation", "model read saturated"),
    ("fresh-move", "no fresh aligned move"),
    ("fresh-move", "insufficient ticks to confirm a fresh move"),
    ("time-remaining", "minimum for entries"),
    ("entry-price-cap", "entry ask"),
    ("below-edge-threshold", "net edge"),
    ("below-confidence", "confidence"),
    ("insufficient-data", "insufficient data"),
]


def classify_reason(reason: str) -> str:
    r = (reason or "").strip()
    if r.startswith("[") and "]" in r:
        r = r.split("]", 1)[1].strip()
    for gate, marker in GATES:
        if marker in r:
            return gate
    return "other"


def _pct(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since-ts", type=float, default=0.0,
                        help="Only consider trades/signals after this unix ts (run start).")
    args = parser.parse_args()

    db = Database(settings.DATABASE_PATH)
    await db.connect()
    try:
        trades = await db.get_all_trades(mode="PAPER")
        signals = await db.get_signals(since_ts=args.since_ts)
        latency = await db.get_latency_events()
    finally:
        await db.close()

    closed = [t for t in trades if t["status"] == "CLOSED" and t.get("realized_pnl_usd") is not None]
    if args.since_ts:
        closed = [t for t in closed if (t.get("entry_ts") or 0) >= args.since_ts]

    print("=" * 62)
    print("VALIDATION RUN ANALYSIS")
    print("=" * 62)

    # -- (a) entry-path split -------------------------------------------------
    print("\n(a) ENTRY-PATH BREAKDOWN (closed trades, net of fees & slippage)")
    print("-" * 62)
    path_buckets: dict[str, dict] = {}
    for t in closed:
        path = t.get("strategy") or "latency_arb"
        b = path_buckets.setdefault(path, {"n": 0, "pnl": 0.0, "wins": 0})
        pnl = t.get("realized_pnl_usd") or 0.0
        b["n"] += 1
        b["pnl"] += pnl
        if pnl > 0:
            b["wins"] += 1
    order = ["latency_arb", "latency_arb_fast", "sum_to_one"]
    for path in order + [p for p in path_buckets if p not in order]:
        b = path_buckets.get(path)
        if not b or b["n"] == 0:
            continue
        label = {
            "latency_arb": "poll path (1s cycle)",
            "latency_arb_fast": "fast path (event-driven)",
            "sum_to_one": "sum-to-one (risk-free)",
        }.get(path, path)
        print(f"  {label:<28} n={b['n']:<4} win={b['wins']/b['n']*100:5.1f}%  "
              f"PnL ${b['pnl']:>9.2f}  expectancy ${b['pnl']/b['n']:>7.3f}")

    total_pnl = sum(t.get("realized_pnl_usd") or 0.0 for t in closed)
    wins = sum(1 for t in closed if (t.get("realized_pnl_usd") or 0.0) > 0)
    losses = sum(t.get("realized_pnl_usd") or 0.0 for t in closed if (t.get("realized_pnl_usd") or 0.0) < 0)
    pf = (sum(t.get("realized_pnl_usd") or 0.0 for t in closed if (t.get("realized_pnl_usd") or 0.0) > 0)
          / abs(losses) if losses else None)
    print("-" * 62)
    print(f"  ALL closed trades: n={len(closed)}  win rate={wins/len(closed)*100:.1f}%  "
          f"net PnL ${total_pnl:.2f}" + (f"  profit factor={pf:.2f}" if pf else ""))

    # -- (b) gate block counts ------------------------------------------------
    print("\n(b) GATE BLOCK COUNTS (signals evaluated, fired=0 by reason)")
    print("-" * 62)
    blocked = [s for s in signals if not s.get("fired")]
    fired = [s for s in signals if s.get("fired")]
    counts = Counter(classify_reason(s.get("reason") or "") for s in blocked)
    print(f"  signals evaluated : {len(signals)}  (fired: {len(fired)}, blocked: {len(blocked)})")
    if counts:
        width = max(len(g) for g in counts)
        for gate, n in counts.most_common():
            print(f"  blocked by {gate:<{width}} : {n}")
    else:
        print("  (no blocked signals in range)")
    if fired:
        fired_edges = [s.get("edge_pct") or 0.0 for s in fired]
        print(f"  fired-signal edge p50={_pct(fired_edges, .5)*100:.2f}%  "
              f"p95={_pct(fired_edges, .95)*100:.2f}%")

    # -- latency context ------------------------------------------------------
    print("\n(c) LATENCY CONTEXT (all latency_events in range)")
    print("-" * 62)
    if latency:
        t2s = [e["tick_to_signal_ms"] for e in latency if e.get("tick_to_signal_ms") is not None]
        t2o = [e["tick_to_order_ms"] for e in latency if e.get("tick_to_order_ms") is not None]
        print(f"  events={len(latency)}  tick->signal p50={_pct(t2s, .5) and round(_pct(t2s, .5), 0)}ms  "
              f"p95={_pct(t2s, .95) and round(_pct(t2s, .95), 0)}ms  "
              f"tick->order p50={_pct(t2o, .5) and round(_pct(t2o, .5), 0)}ms  "
              f"p95={_pct(t2o, .95) and round(_pct(t2o, .95), 0)}ms")
    else:
        print("  (no latency events in range)")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
