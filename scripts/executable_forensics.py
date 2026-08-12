"""Executable-profit forensics — separates THEORETICAL repricing from
EXECUTABLE profit (recommendation from the 2026-08-12 external review).

The lag strategy's premise is "Polymarket eventually follows Binance". But
"the market moved the right way" is NOT the same as "we could have made
money" — the book can reprice before our order reaches it, the fill can
slip, fees can eat the edge. This report traces the FULL chain per closed
directional trade:

    decision edge (signal)
      -> book age at decision (poly_book_age_s)
      -> entry latency (latency_events.tick_to_order_ms)
      -> decision ask -> fill ask (slippage)
      -> fees
      -> exit reason / hold
      -> net PnL

...and answers the PFE question:

    Of the detected gaps, what fraction were actually tradeable after
    latency, spread, fees and depth — and of the trades we took, what
    fraction were net profitable?

Also classifies EVERY early exit (EDGE_REVERSAL + GAP_EXPIRED) as premature
vs protective from the exit_probes data, so the gap-timed exit's selection
bias is measured, not argued: GAP_EXPIRED is built on the median of REPRICE
WINNERS — did it cut losers (protective) or also cut slow winners
(premature)?

Run against the local DB or a restored Telegram backup:

    python scripts/executable_forensics.py [--db storage/arb_bot.db]

Pure measurement — no thresholds are changed and nothing gates trading.
"""
from __future__ import annotations

import argparse
import asyncio
from statistics import mean, median
from typing import Any, Optional

from config.settings import Settings
from engine.exit_forensics import classify_early_exits
from storage.db import Database


def _last_signal_before(
    signals: list[dict[str, Any]], market_id: str, entry_ts: float,
) -> Optional[dict[str, Any]]:
    """The signal that fired this trade: the last logged evaluation for this
    market whose timestamp is within 10 min before the entry (the paper
    broker stores signal_id=None, so trades are matched to their decision
    signal by market + time — the same convention get_signals documents)."""
    best: Optional[dict[str, Any]] = None
    for s in signals:
        ts = s.get("ts") or 0.0
        if s.get("market_id") == market_id and entry_ts - 600 <= ts <= entry_ts + 5:
            if best is None or ts > (best.get("ts") or 0.0):
                best = s
    return best


def _latency_for(
    latency_rows: list[dict[str, Any]], market_id: str, entry_ts: float,
) -> Optional[dict[str, Any]]:
    """The entry-latency measurement for this trade: the latency row for this
    market whose order_submitted_at is closest to the trade's entry time
    (within 10s)."""
    best: Optional[dict[str, Any]] = None
    for r in latency_rows:
        if r.get("market_id") != market_id or not r.get("order_submitted_at"):
            continue
        if abs((r.get("order_submitted_at") or 0.0) - entry_ts) <= 10.0:
            if best is None or abs((r.get("order_submitted_at") or 0.0) - entry_ts) < \
                    abs((best.get("order_submitted_at") or 0.0) - entry_ts):
                best = r
    return best


def build_trade_table(
    trades: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    latency_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """One row per CLOSED directional trade, joined with its decision-time
    signal and entry-latency measurement — the full executable-profit chain.
    Sum-to-one legs are excluded (they are outcome-agnostic holds, not
    convergence trades). Missing joins are tolerated (None fields) so a
    sparse DB still produces a report."""
    rows: list[dict[str, Any]] = []
    for t in trades:
        if t.get("status") != "CLOSED":
            continue
        if (t.get("strategy") or "latency_arb") == "sum_to_one":
            continue
        entry_ts = t.get("entry_ts") or 0.0
        sig = _last_signal_before(signals, t.get("market_id") or "", entry_ts)
        lat = _latency_for(latency_rows, t.get("market_id") or "", entry_ts)
        entry_cost = (t.get("size_usd") or 0.0) + (t.get("fee_usd") or 0.0)
        net = t.get("realized_pnl_usd") or 0.0
        rows.append({
            "id": t.get("id"),
            "asset": t.get("asset"),
            "side": t.get("side"),
            "exit_reason": t.get("exit_reason"),
            "entry_price": t.get("entry_price"),
            "exit_price": t.get("exit_price"),
            "size_usd": t.get("size_usd"),
            "fee_usd": t.get("fee_usd"),
            "net_pnl_usd": net,
            "net_return_pct": (net / entry_cost) if entry_cost else None,
            "decision_edge_pct": sig.get("edge_pct") if sig else None,
            "book_age_s": sig.get("poly_book_age_s") if sig else None,
            "tick_to_order_ms": lat.get("tick_to_order_ms") if lat else None,
            "slippage_pct": t.get("slippage_pct"),
            "mfe_pct": t.get("mfe_pct"),
            "mae_pct": t.get("mae_pct"),
        })
    return rows


def _avg(vals: list[Optional[float]]) -> Optional[float]:
    clean = [v for v in vals if v is not None]
    return mean(clean) if clean else None


def _med(vals: list[Optional[float]]) -> Optional[float]:
    clean = [v for v in vals if v is not None]
    return median(clean) if clean else None


def summarize(
    table: list[dict[str, Any]],
    closed_all: list[dict[str, Any]],
    probes: list[dict[str, Any]],
    reprice_target: float,
) -> dict[str, Any]:
    """Aggregate the executable-profit picture from the per-trade table:
    win rate and net PnL, edge decay (decision edge vs realized return),
    entry latency, book age at decision, slippage, fees, and the premature/
    protective split of every early exit."""
    n = len(table)
    wins = sum(1 for r in table if r["net_pnl_usd"] > 0)
    net = sum(r["net_pnl_usd"] for r in table)
    fees = sum(r["fee_usd"] or 0.0 for r in table)
    classification = classify_early_exits(
        closed_all, probes, reprice_target,
        exit_reasons=("EDGE_REVERSAL", "GAP_EXPIRED"),
    )
    gap_exits = [t for t in closed_all if t.get("exit_reason") == "GAP_EXPIRED"]

    def _bucket_n(bucket):
        return sum(1 for t, _, _ in bucket if t.get("exit_reason") == "GAP_EXPIRED")

    by_reason: dict[str, float] = {}
    for t in closed_all:
        reason = t.get("exit_reason") or "?"
        by_reason[reason] = by_reason.get(reason, 0.0) + (t.get("realized_pnl_usd") or 0.0)

    avg_edge = _avg([r["decision_edge_pct"] for r in table])
    avg_return = _avg([r["net_return_pct"] for r in table])

    return {
        "n": n,
        "wins": wins,
        "win_rate_pct": wins / n * 100.0 if n else None,
        "net_pnl_usd": net,
        "fees_usd": fees,
        "avg_net_return_pct": avg_return,
        "avg_decision_edge_pct": avg_edge,
        "edge_decay_pct": None if not n else (
            (avg_edge or 0.0) - (avg_return or 0.0)
        ),
        "median_tick_to_order_ms": _med([r["tick_to_order_ms"] for r in table]),
        "avg_book_age_s": _avg([r["book_age_s"] for r in table]),
        "avg_slippage_pct": _avg([r["slippage_pct"] for r in table]),
        "by_reason": dict(sorted(by_reason.items(), key=lambda kv: -kv[1])),
        "early_exits": {
            "n": len(classification["reversals"]),
            "premature_n": len(classification["premature"]),
            "protective_n": len(classification["protective"]),
            "held_won_n": len(classification["held_won"]),
            "no_data_n": len(classification["no_data"]),
        },
        "gap_expired": {
            "n": len(gap_exits),
            "premature_n": _bucket_n(classification["premature"]),
            "held_won_n": _bucket_n(classification["held_won"]),
            "protective_n": _bucket_n(classification["protective"]),
            "no_data_n": _bucket_n(classification["no_data"]),
            "net_pnl_usd": sum((t.get("realized_pnl_usd") or 0) for t in gap_exits),
        },
        "reprice_target": reprice_target,
    }


def _pct(v: Optional[float]) -> str:
    return f"{v * 100:.1f}%" if v is not None else "—"


def _ms(v: Optional[float]) -> str:
    return f"{v:.0f}ms" if v is not None else "—"


def format_report(s: dict[str, Any]) -> str:
    lines = [
        "=" * 72,
        "EXECUTABLE-PROFIT FORENSICS — theoretical reprice vs real money",
        "=" * 72,
        f"Closed directional trades : {s['n']}   wins: {s['wins']} "
        f"({_pct(s['win_rate_pct'])})   net: ${s['net_pnl_usd']:+.2f}   fees: ${s['fees_usd']:.2f}",
        "",
        "THEORETICAL edge vs EXECUTABLE result:",
        f"  avg decision edge (signal) : {_pct(s['avg_decision_edge_pct'])}",
        f"  avg net return per trade   : {_pct(s['avg_net_return_pct'])}",
        f"  EDGE DECAY (lost to fees, slippage, spread, latency): {_pct(s['edge_decay_pct'])}",
        "",
        "EXECUTION quality:",
        f"  median tick -> order       : {_ms(s['median_tick_to_order_ms'])}   "
        f"(arbitrage window ~2s; platform taker delay +250ms)",
        f"  avg Polymarket book age    : {s['avg_book_age_s']}s   "
        f"(WS treats <5s as fresh; entry on a 4.5s-old book is stale)",
        f"  avg fill slippage          : {_pct(s['avg_slippage_pct'])}",
        "",
        "Per exit reason (net PnL):",
    ]
    for reason, pnl in (s.get("by_reason") or {}).items():
        lines.append(f"  {reason:<16} {pnl:+10.2f}")

    ex = s.get("early_exits") or {}
    lines += [
        "",
        f"EARLY EXITS (EDGE_REVERSAL + GAP_EXPIRED, n={ex.get('n', 0)}):",
        f"  PREMATURE (repriced to +{s.get('reprice_target', 0.10):.0%} after we left): {ex.get('premature_n', 0)}",
        f"  Held side WON at settlement:  {ex.get('held_won_n', 0)}",
        f"  Protective (kept falling):    {ex.get('protective_n', 0)}",
        f"  No probe data yet:            {ex.get('no_data_n', 0)}",
    ]
    gap = s.get("gap_expired") or {}
    if gap.get("n"):
        lines += [
            "",
            f"GAP_EXPIRED (n={gap['n']}, net ${gap['net_pnl_usd']:+.2f}) — the selection-bias check:",
            f"  PREMATURE (market hit target after we left): {gap.get('premature_n', 0)}",
            f"  Protective (kept falling):                   {gap.get('protective_n', 0)}",
            f"  No probe data yet:                           {gap.get('no_data_n', 0)}",
        ]
    lines += [
        "",
        "Measurement only — no thresholds changed; the run freeze stays in force.",
    ]
    return "\n".join(lines)


def format_trade_rows(table: list[dict[str, Any]]) -> str:
    lines = []
    for r in table:
        edge = _pct(r["decision_edge_pct"])
        lat = _ms(r["tick_to_order_ms"])
        book = f"{r['book_age_s']:.1f}s" if r["book_age_s"] is not None else "—"
        slip = _pct(r["slippage_pct"])
        ret = _pct(r["net_return_pct"])
        lines.append(
            f"  #{r['id']:<4} {str(r.get('asset')):<3} {str(r.get('side')):<3} "
            f"edge {edge:<7} lat {lat:<7} book {book:<6} slip {slip:<7} "
            f"{str(r.get('exit_reason')):<12} net {r['net_pnl_usd']:+8.2f} ({ret})"
        )
    return "\n".join(lines) if lines else "  (no closed directional trades with data yet)"


async def analyze(db_path: str) -> int:
    db = Database(db_path)
    await db.connect()
    try:
        settings = Settings(_env_file=None)
        all_trades = await db.get_all_trades(mode="PAPER")
        closed = [t for t in all_trades if t.get("status") == "CLOSED"]
        signals = await db.get_signals()
        latency_rows = await db.get_latency_events()
        probes = await db.get_exit_probes()

        table = build_trade_table(all_trades, signals, latency_rows)
        summary = summarize(table, closed, probes, settings.REPRICE_EXIT_GAIN_PCT)

        print(format_report(summary))
        print()
        print(format_trade_rows(table))
        return 0
    finally:
        await db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=str, default=Settings(_env_file=None).DATABASE_PATH,
                        help="SQLite database path (default: settings.DATABASE_PATH).")
    args = parser.parse_args()
    return asyncio.run(analyze(args.db))


if __name__ == "__main__":
    raise SystemExit(main())
