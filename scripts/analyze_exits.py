"""Classify every early exit using the exit_probes table (measurement data).

This is the premature-exit detector for the round-trip strategy: after the
bot exits early (REPRICE / TAKE_PROFIT / EDGE_REVERSAL), _exit_probe_loop
samples the held token's price at T+5/15/30/60/120s and at settlement. This
script answers the question that decides whether EDGE_REVERSAL protects
capital or cuts good arbitrages:

  "After we exited, did the market reprice to a win?"

For every closed directional trade with probes, a trade is a PREMATURE cut
if any post-exit probe reaches the REPRICE target (REPRICE_EXIT_GAIN_PCT)
above entry — the convergence the strategy exists to bank happened after we
left. It also reports MFE/MAE per exit reason: how much profit was available
and how deep positions dipped, win or loss.

Run against the local DB or a restored Telegram backup:

    python scripts/analyze_exits.py [--db storage/arb_bot.db]
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from statistics import mean
from typing import Optional

from config.settings import Settings
from storage.db import Database


def _recovery_pct(quote_price: float, entry_price: float) -> float:
    return (quote_price - entry_price) / entry_price if entry_price else 0.0


async def analyze(db_path: str) -> int:
    db = Database(db_path)
    await db.connect()
    try:
        settings = Settings(_env_file=None)
        reprice_target = settings.REPRICE_EXIT_GAIN_PCT
        trades = [t for t in await db.get_all_trades(mode="PAPER") if t.get("status") == "CLOSED"]
        probes = await db.get_exit_probes()
        by_trade: dict[int, list] = defaultdict(list)
        for p in probes:
            by_trade[p["trade_id"]].append(p)

        print("=" * 70)
        print("EXIT FORENSICS — premature-exit analysis (measurement data)")
        print("=" * 70)
        print(f"closed trades: {len(trades)}   probed trades: {len({p['trade_id'] for p in probes})}   "
              f"reprice target: {reprice_target:.0%}")

        # --- 1. Exit-reason summary with MFE/MAE ---------------------------
        print("\n--- per exit reason (net PnL, excursion) ---")
        rows: dict[str, list] = defaultdict(list)
        for t in trades:
            rows[t.get("exit_reason") or "?"].append(t)
        for reason, group in sorted(rows.items(), key=lambda kv: -sum(t["realized_pnl_usd"] or 0 for t in kv[1])):
            mfes = [t["mfe_pct"] for t in group if t.get("mfe_pct") is not None]
            maes = [t["mae_pct"] for t in group if t.get("mae_pct") is not None]
            print(f"  {reason:<14} n={len(group):<4} net={sum(t['realized_pnl_usd'] or 0 for t in group):+8.2f}  "
                  f"avg MFE={mean(mfes):+6.1%}  avg MAE={mean(maes):+6.1%}" if mfes and maes else
                  f"  {reason:<14} n={len(group):<4} net={sum(t['realized_pnl_usd'] or 0 for t in group):+8.2f}  (no excursion data)")

        # --- 2. EDGE_REVERSAL classification --------------------------------
        reversals = [t for t in trades if t.get("exit_reason") == "EDGE_REVERSAL"]
        print(f"\n--- EDGE_REVERSAL classification (n={len(reversals)}) ---")
        premature, held_won, protective, no_data = [], [], [], []
        for t in reversals:
            samples = sorted(by_trade.get(t["id"], []), key=lambda p: p["ts"])
            if not samples:
                no_data.append(t)
                continue
            max_recovery = max(_recovery_pct(p["quote_price"], t["entry_price"]) for p in samples
                               if p["sample_label"] != "P_SETTLED")
            settled = next((p for p in samples if p["sample_label"] == "P_SETTLED"), None)
            if max_recovery >= reprice_target:
                premature.append((t, max_recovery, settled))
            elif settled is not None and settled["quote_price"] >= 1.0:
                held_won.append((t, max_recovery, settled))
            else:
                protective.append((t, max_recovery, settled))

        loss_dollars = sum(t["realized_pnl_usd"] or 0 for t in reversals)
        prem_dollars = sum(t["realized_pnl_usd"] or 0 for t, _, _ in premature)
        print(f"  PREMATURE cut — market hit +{reprice_target:.0%} after we left: {len(premature)} "
              f"trades, ${prem_dollars:+.2f} of ${loss_dollars:+.2f} loss dollars "
              f"({100 * prem_dollars / loss_dollars if loss_dollars else 0:.0f}%)")
        print(f"  Held side WON at settlement (cut before the payout): {len(held_won)} trades")
        print(f"  Protective cut (kept falling / held side lost):      {len(protective)} trades")
        print(f"  No probe data:                                       {len(no_data)} trades")
        for t, rec, settled in premature:
            print(f"    trade {t['id']} {t['side']}@{t['entry_price']:.2f} -> {t['exit_price']:.2f} "
                  f"({t['realized_pnl_usd']:+.2f}); after exit reached {rec:+.0%}"
                  f"{' and held side WON at settlement' if settled and settled['quote_price'] >= 1.0 else ''}")

        # --- 3. How much profit did winners leave on the table? -------------
        print("\n--- REPRICE wins: MFE vs banked gain ---")
        reprices = [t for t in trades if t.get("exit_reason") == "REPRICE" and t.get("mfe_pct") is not None]
        if reprices:
            banked = mean((t["exit_price"] - t["entry_price"]) / t["entry_price"] for t in reprices)
            mfe = mean(t["mfe_pct"] for t in reprices)
            mae = mean(t["mae_pct"] for t in reprices)
            print(f"  n={len(reprices)}  avg banked gain={banked:+.1%}  avg MFE={mfe:+.1%}  "
                  f"avg MAE={mae:+.1%}  (MFE - banked = {mfe - banked:+.1%} left on the table)")
            print("  (a big MFE-minus-banked gap means the +10% exit capped trades that ran much further)")
        else:
            print("  (no REPRICE wins with excursion data yet)")

        # --- 4. Summary verdict ----------------------------------------------
        print("\n--- verdict ---")
        if premature:
            print(f"  {len(premature)} of {len(reversals)} EDGE_REVERSAL exits would have hit the reprice target "
                  f"after leaving — the reversal is cutting good convergence at least some of the time.")
        elif reversals:
            print(f"  All {len(reversals)} EDGE_REVERSAL exits stayed below the reprice target after leaving — "
                  "the reversal is protective in the current sample.")
        else:
            print("  No EDGE_REVERSAL exits in the sample yet.")
        print("  Measurement data only — no thresholds changed; the run freeze stays in force.")
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
