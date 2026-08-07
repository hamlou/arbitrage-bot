"""
Gate forensics — which signal gates are blocking trades, and whether the
blocked signals were CORRECT refusals or MISSED opportunities.

Pure read-only analysis of the `signals` table. Every evaluated signal is
logged with its model read (implied_prob), the Polymarket mid at evaluation
(polymarket_prob), the side the model wanted, and the reason it was blocked.

How "market agreed" is judged: for a blocked signal with side S and implied
probability P, the market agreed with the model if Polymarket's quoted
probability moved TOWARD P within the following AGREEMENT_WINDOW_S (120s).
The bot evaluates every market roughly once per second, so later signals on
the SAME market ARE a price history — no extra recording needed.

Usage:
    python scripts/gate_forensics.py [--db storage/arb_bot.db] [--hours 24]
    python scripts/gate_forensics.py --hours 6

Output: per-gate table of blocked counts + agreement rates, so gate tuning
becomes a measured decision ("this gate blocks 230 signals but the market
agreed with 78% of them — it's eating real edges") instead of a vibe.
"""
from __future__ import annotations

import argparse
import bisect
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "storage" / "arb_bot.db"

AGREEMENT_WINDOW_S = 120.0
AGREEMENT_MOVE = 0.02  # the market must move at least 2 cents toward the model
# Signals on near-resolved markets (quote pinned at 0.85+ or 0.15-) "agree"
# with the model trivially — the market is already decided, so agreement there
# is not evidence the read was tradable. Excluded from the agreement stat
# (still counted, in the 'pinned' column) so it can't inflate a gate's
# agreed% (reviewed 2026-08-07: the saturation bucket showed 94.8% "agreed"
# mostly from pinned markets).
PINNED_PROB = 0.85

# reason-prefix -> gate name (checked in order; first match wins)
GATE_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("entry_price_cap", ("entry ask",)),
    ("fresh_move", ("no fresh aligned move", "insufficient ticks to confirm a fresh move")),
    ("min_time_remaining", ("only ")),
    ("saturation", ("model read saturated",)),
    ("edge_threshold", ("net edge",)),
    ("min_confidence", ("confidence",)),
    ("insufficient_data", ("insufficient data",)),
    ("cross_exchange", ("cross_exchange_disagreement",)),
    ("other", ()),
]


def classify_gate(reason: str) -> str:
    reason = (reason or "").strip()
    # The engine prepends an audit prefix like "[fair_value] " or
    # "[momentum_fallback] " to most reasons — strip it before matching.
    if reason.startswith("[") and "] " in reason[:40]:
        reason = reason.split("] ", 1)[1]
    for gate, prefixes in GATE_PATTERNS:
        if any(reason.startswith(p) for p in prefixes):
            return gate
    return "other"


def _pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p))]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="path to arb_bot.db")
    parser.add_argument("--hours", type=float, default=24.0, help="analyze only the last N hours")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"NO-GO: database not found at {db_path}")
        raise SystemExit(1)

    since = time.time() - args.hours * 3600.0
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT ts, market_id, implied_prob, polymarket_prob, edge_pct, fired, reason "
        "FROM signals WHERE ts >= ? ORDER BY market_id, ts",
        (since,),
    ).fetchall()
    con.close()

    if not rows:
        print(f"No signals found in the last {args.hours:g}h — nothing to analyze.")
        return

    # Index by market: sorted (ts, row) so per-signal lookahead is a bisect.
    by_market: dict[str, list[tuple[float, sqlite3.Row]]] = defaultdict(list)
    for r in rows:
        by_market[r["market_id"]].append((r["ts"], r))

    fired = sum(1 for r in rows if r["fired"])
    blocked = [r for r in rows if not r["fired"]]

    # Per-gate tallies for blocked signals that had a side + model read.
    gate_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"blocked": 0, "agreed": 0, "pinned": 0})
    agree_pcts: dict[str, list[float]] = defaultdict(list)

    for r in blocked:
        gate = classify_gate(r["reason"] or "")
        gate_stats[gate]["blocked"] += 1
        # The signals table stores no side column — derive it exactly like the
        # engine does: the model leans YES when implied > market quote.
        implied = r["implied_prob"]
        poly = r["polymarket_prob"]
        if implied is None or poly is None or implied == poly:
            continue
        if poly >= PINNED_PROB or poly <= 1.0 - PINNED_PROB:
            # Near-resolved market — agreement would be trivial, not evidence.
            gate_stats[gate]["pinned"] += 1
            continue
        side = "YES" if implied > poly else "NO"
        market_rows = by_market.get(r["market_id"], [])
        if not market_rows:
            continue
        ts_list = [t for t, _ in market_rows]
        lo = bisect.bisect_left(ts_list, r["ts"])
        hi = bisect.bisect_right(ts_list, r["ts"] + AGREEMENT_WINDOW_S)
        if hi - lo <= 1:
            continue  # no later observations on this market
        window_probs = [row["polymarket_prob"] for t, row in market_rows[lo:hi]]
        if not any(p is not None for p in window_probs):
            continue
        base = poly
        if side == "YES":
            # Market agrees if the quoted YES probability ROSE toward implied.
            agreed = max(p for p in window_probs if p is not None) >= base + AGREEMENT_MOVE
        else:
            agreed = min(p for p in window_probs if p is not None) <= base - AGREEMENT_MOVE
        if agreed:
            gate_stats[gate]["agreed"] += 1
            agree_pcts[gate].append(r["edge_pct"] or 0.0)

    print(f"Signals in last {args.hours:g}h: {len(rows)}  (fired={fired}, blocked={len(blocked)})")
    print(f"Agreement = market moved >= {AGREEMENT_MOVE:.0%} toward the model within "
          f"{AGREEMENT_WINDOW_S:.0f}s of a blocked signal.")
    print()
    print(f"{'gate':<22}{'blocked':>9}{'pinned':>8}{'agreed':>9}{'agreed%':>9}{'median edge (agreed)':>22}")
    print("-" * 82)
    for gate in sorted(gate_stats, key=lambda g: -gate_stats[g]["blocked"]):
        s = gate_stats[gate]
        if not s["blocked"]:
            continue
        eligible = s["blocked"] - s["pinned"]
        agreed_pct = s["agreed"] / eligible * 100.0 if eligible else 0.0
        med = _pct(agree_pcts.get(gate, []), 0.5)
        print(f"{gate:<22}{s['blocked']:>9}{s['pinned']:>8}{s['agreed']:>9}"
              f"{agreed_pct:>8.1f}%{med:>19.3f}")

    print()
    print("How to read it: 'pinned' = signals on near-resolved markets (quote at "
          f"{PINNED_PROB:.0%}+ or {1 - PINNED_PROB:.0%}-) where agreement is trivial and "
          "excluded from agreed%. A gate with a HIGH agreed% on eligible signals is "
          "blocking real edges (the market moved toward the model) — a candidate to "
          "loosen. A LOW agreed% means it's correctly refusing bad reads — leave it. "
          "Note: agreement measures DIRECTION, not profit — always read it next to the "
          "entry price and fee-aware edge before touching a threshold.")


if __name__ == "__main__":
    main()
