"""
Settle any paper positions that are still OPEN on markets which have actually
resolved. This is a one-shot repair/audit tool: it re-runs the real
PaperBroker.settle_position() path against the live Gamma API so the balance,
win rate, and per-trade PnL become truthful after the settlement bug that
left positions OPEN forever (fixed 2026-08-06).

Safe: PAPER trades only, uses the exact same settle code the bot uses, prints
every settlement. Never touches live mode, never opens new trades.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Scripts run from anywhere; make the project root importable (same pattern as
# the other scripts/ helpers).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings
from data.polymarket_feed import PolymarketFeed
from engine.broker_paper import PaperBroker
from storage.db import Database

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("settle_stuck")


async def main() -> None:
    db = Database(settings.DATABASE_PATH)
    await db.connect()
    feed = PolymarketFeed(min_liquidity_usd=0.0)
    broker = PaperBroker(
        db=db,
        feed=feed,
        starting_balance_usd=settings.STARTING_PAPER_BALANCE_USD,
        simulated_fill_latency_s=settings.SIMULATED_FILL_LATENCY_S,
        min_order_size_usd=settings.MIN_ORDER_SIZE_USD,
        tick_size=settings.TICK_SIZE,
    )
    await broker.load_open_positions()

    open_trades = await db.get_open_trades(mode="PAPER")
    market_ids = sorted({t["market_id"] for t in open_trades})
    if not market_ids:
        print("No open paper trades to settle.")
        await feed.aclose()
        await db.close()
        return

    print(f"{len(open_trades)} open paper trade(s) across {len(market_ids)} market(s):")
    for t in open_trades:
        print(f"  trade {t['id']}: market {t['market_id']} {t['side']} @ {t['entry_price']} "
              f"(${t['size_usd']:.2f}) opened {t['entry_ts']}")
    print()

    total_pnl = 0.0
    for market_id in market_ids:
        market = await feed.get_market_by_id(market_id)
        if market is None:
            print(f"  market {market_id}: not found on Gamma — skipped")
            continue
        outcome = await feed.get_market_outcome(market_id)
        print(f"  market {market_id}: closed={market.resolved} outcome={outcome}")
        if outcome is None:
            print(f"    not resolved yet — leaving open")
            continue
        pnl = await broker.settle_position(market)
        if pnl is not None:
            total_pnl += pnl
            print(f"    settled, PnL ${pnl:.2f}")
        else:
            print(f"    settle returned None (no tracked open trade?)")

    balance = await broker.get_balance()
    closed = [t for t in await db.get_all_trades(mode="PAPER") if t["status"] == "CLOSED"]
    pnls = [t["realized_pnl_usd"] or 0.0 for t in closed]
    wins = sum(1 for p in pnls if p > 0)
    print()
    print(f"=== AFTER SETTLEMENT ===")
    print(f"Balance: ${balance:,.2f}")
    print(f"Closed trades: {len(closed)}")
    print(f"Total realized PnL: ${sum(pnls):,.2f}")
    print(f"Win rate: {wins}/{len(closed)} = {wins/len(closed)*100:.0f}%" if closed else "n/a")

    await feed.aclose()
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
