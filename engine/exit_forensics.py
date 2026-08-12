"""
Shared exit-forensics computation — the single source of truth for the
premature-vs-protective classification, used by BOTH:

  - scripts/analyze_exits.py  (manual deep-dive report)
  - the daily Telegram forensics digest (main.py)

...so the two can never drift. Pure computation, no I/O, no DB access — the
caller passes in the rows it already fetched. Measurement only: nothing here
gates or changes trading behavior.

The question this answers: after the bot exits early (REPRICE / TAKE_PROFIT /
EDGE_REVERSAL), did the market reprice to a win after we left? A trade is a
PREMATURE cut if any post-exit probe reaches the REPRICE target above entry —
the convergence the strategy exists to bank happened after we sold.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional


def recovery_pct(quote_price: float, entry_price: float) -> float:
    return (quote_price - entry_price) / entry_price if entry_price else 0.0


def classify_early_exits(
    closed_trades: list[dict[str, Any]],
    probes: list[dict[str, Any]],
    reprice_target: float,
) -> dict[str, Any]:
    """
    Classify every EDGE_REVERSAL exit as premature / held-won / protective.

    Returns a dict with the four buckets, each a list of (trade, max_recovery,
    settled_probe_or_None) tuples, plus the underlying inputs.
    """
    by_trade: dict[int, list] = defaultdict(list)
    for p in probes:
        by_trade[p["trade_id"]].append(p)

    reversals = [t for t in closed_trades if t.get("exit_reason") == "EDGE_REVERSAL"]
    premature: list = []
    held_won: list = []
    protective: list = []
    no_data: list = []
    for t in reversals:
        samples = sorted(by_trade.get(t["id"], []), key=lambda p: p["ts"])
        if not samples:
            no_data.append(t)
            continue
        max_recovery = max(
            recovery_pct(p["quote_price"], t["entry_price"])
            for p in samples if p["sample_label"] != "P_SETTLED"
        )
        settled = next((p for p in samples if p["sample_label"] == "P_SETTLED"), None)
        if max_recovery >= reprice_target:
            premature.append((t, max_recovery, settled))
        elif settled is not None and settled["quote_price"] >= 1.0:
            held_won.append((t, max_recovery, settled))
        else:
            protective.append((t, max_recovery, settled))

    return {
        "reversals": reversals,
        "premature": premature,
        "held_won": held_won,
        "protective": protective,
        "no_data": no_data,
        "reprice_target": reprice_target,
    }


def _lag_stats(lag_events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-asset lag measurements from lag_events: sample count, median lag
    (ms), reprice rate (1 - timed-out fraction), and direction accuracy —
    of the moves whose implied token actually repriced, what fraction moved
    the RIGHT way. This is the empirical number that will decide whether
    gap-timed ENTRIES are ever safe (added 2026-08-12); pure measurement."""
    by_asset: dict[str, list] = defaultdict(list)
    for e in lag_events:
        by_asset.setdefault(e.get("asset") or "?", []).append(e)

    stats: dict[str, dict[str, Any]] = {}
    for asset, events in by_asset.items():
        n = len(events)
        lags = sorted(e["lag_ms"] for e in events if e.get("lag_ms") is not None)
        repriced = sum(1 for e in events if not e.get("timed_out"))
        moved = [e for e in events if e.get("poly_move_pct") is not None]
        correct = sum(
            1 for e in moved
            if (e.get("move_dir") == "UP" and e["poly_move_pct"] > 0)
            or (e.get("move_dir") == "DOWN" and e["poly_move_pct"] < 0)
        )
        stats[asset] = {
            "n": n,
            "median_lag_ms": lags[len(lags) // 2] if lags else None,
            "repriced_pct": repriced / n * 100.0 if n else None,
            "correct_dir_pct": correct / len(moved) * 100.0 if moved else None,
        }
    return stats


def build_digest_summary(
    all_trades: list[dict[str, Any]],
    probes: list[dict[str, Any]],
    reprice_target: float,
    freeze_min_trades: int,
    freeze_min_days: float,
    live_min_trades: int,
    live_min_days: float,
    live_min_distinct_days: int,
    lag_events: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """
    Compact summary for the daily Telegram digest (and nothing else). All
    values are counts/splits computed from data the bot already collects —
    pure reporting, zero threshold risk. Returns a flat dict the formatter
    renders; never raises on weird rows (missing keys are tolerated).
    """
    closed = [t for t in all_trades if t.get("status") == "CLOSED"]
    classification = classify_early_exits(closed, probes, reprice_target)

    # Days elapsed + distinct trading days: same logic validate_paper_run.py
    # uses, kept inline so the digest and the gate agree by construction.
    days_elapsed = 0.0
    distinct_days = 0
    if closed:
        first_ts = min(t.get("entry_ts") or 0 for t in closed)
        if first_ts:
            days_elapsed = (datetime.now(timezone.utc).timestamp() - first_ts) / 86400
        day_set = {
            datetime.fromtimestamp(t.get("entry_ts") or 0, tz=timezone.utc).strftime("%Y-%m-%d")
            for t in closed if t.get("entry_ts")
        }
        distinct_days = len(day_set)

    prem_dollars = sum((t.get("realized_pnl_usd") or 0) for t, _, _ in classification["premature"])
    reversal_dollars = sum((t.get("realized_pnl_usd") or 0) for t in classification["reversals"])

    # Per-exit-reason net PnL (the first thing anyone asks: what's winning).
    by_reason: dict[str, float] = defaultdict(float)
    for t in closed:
        by_reason[t.get("exit_reason") or "?"] += t.get("realized_pnl_usd") or 0.0

    return {
        "closed_trades": len(closed),
        "days_elapsed": days_elapsed,
        "distinct_trading_days": distinct_days,
        "net_pnl_usd": sum((t.get("realized_pnl_usd") or 0) for t in closed),
        "by_reason": dict(sorted(by_reason.items(), key=lambda kv: -kv[1])),
        "reversals_n": len(classification["reversals"]),
        "premature_n": len(classification["premature"]),
        "premature_dollars": prem_dollars,
        "reversal_dollars": reversal_dollars,
        "held_won_n": len(classification["held_won"]),
        "protective_n": len(classification["protective"]),
        "no_data_n": len(classification["no_data"]),
        "freeze_min_trades": freeze_min_trades,
        "freeze_min_days": freeze_min_days,
        "live_min_trades": live_min_trades,
        "live_min_days": live_min_days,
        "live_min_distinct_days": live_min_distinct_days,
        "lag_stats": _lag_stats(lag_events or []),
    }
