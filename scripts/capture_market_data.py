"""
Standalone recorder: captures live Binance ticks + Polymarket order books to
a JSONL file for the replay backtest (scripts/replay_arb.py). This is the
data-collection half of measuring the arbitrage window empirically — it is
what turns "we think the window is 2s" into "here is the measured window."

Run it for an hour or two whenever you want a fresh measurement:

    python scripts/capture_market_data.py --hours 1
    python scripts/capture_market_data.py --hours 2 --out storage/captures/evening.jsonl

Output line schema (one JSON object per line):
    {"t": <epoch>, "type": "market", "asset": "BTC", "market_id": "...",
     "token_yes": "...", "token_no": "...", "duration_minutes": 5, "question": "..."}
    {"t": <epoch>, "type": "binance", "symbol": "BTCUSDT", "price": 65000.5}
    {"t": <epoch>, "type": "poly_book", "token_id": "...",
     "bids": [[price, size], ...], "asks": [[price, size], ...]}

Books are written on change (sampled every ~250ms); Binance ticks are written
only when the price changes. It is safe to run alongside the running bot —
it opens its own connections and writes only to its own file.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Optional

from data.binance_feed import BinanceFeed
from data.polymarket_feed import PolymarketFeed
from data.polymarket_ws_feed import PolymarketWSFeed

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "storage" / "captures"
SAMPLE_INTERVAL_S = 0.25  # how often the book sampler looks for changes


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=1.0, help="capture duration in hours")
    parser.add_argument("--out", default=None, help="output .jsonl path (default: storage/captures/capture_<ts>.jsonl)")
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT", help="comma-separated Binance symbols")
    args = parser.parse_args()

    out_dir = Path(args.out).parent if args.out else DEFAULT_OUT_DIR
    out_path = Path(args.out) if args.out else out_dir / f"capture_{int(time.time())}.jsonl"
    out_dir.mkdir(parents=True, exist_ok=True)

    write_lock = asyncio.Lock()
    f = open(out_path, "w", encoding="utf-8")

    async def write(record: dict) -> None:
        async with write_lock:
            f.write(json.dumps(record, default=str) + "\n")
            f.flush()

    # -- discover the markets we care about (BTC/ETH up-or-down windows) --
    feed = PolymarketFeed(min_liquidity_usd=0.0)
    markets = await feed.discover_active_markets()
    wanted_assets = {s[:-4] for s in args.symbols.split(",") if s.endswith("USDT")}
    wanted_symbols = set(args.symbols.split(","))
    markets = [m for m in markets if m.asset in wanted_assets]
    if not markets:
        print("No active BTC/ETH up-or-down markets right now — nothing to capture.")
        await feed.aclose()
        f.close()
        raise SystemExit(1)

    token_ids: list[str] = []
    for m in markets:
        token_ids.extend([m.token_id_yes, m.token_id_no])
        await write({
            "t": time.time(), "type": "market", "asset": m.asset,
            "market_id": m.market_id, "token_yes": m.token_id_yes,
            "token_no": m.token_id_no, "duration_minutes": m.duration_minutes,
            "question": m.question, "liquidity_usd": m.liquidity_usd,
            "expires_at_ts": m.expires_at_ts,
        })
    print(f"Capturing {len(markets)} markets, {len(token_ids)} tokens -> {out_path}")
    print(f"Duration: {args.hours:g}h. Ctrl+C to stop early.")

    ws_feed = PolymarketWSFeed(asset_ids=token_ids)
    binance_feed = BinanceFeed()

    last_binance_price: dict[str, float] = {}
    last_book_sig: dict[str, tuple] = {}

    async def binance_consumer() -> None:
        async for update in binance_feed.stream():
            if update.symbol not in wanted_symbols:
                continue
            if last_binance_price.get(update.symbol) == update.price:
                continue
            last_binance_price[update.symbol] = update.price
            await write({"t": update.received_at, "type": "binance",
                         "symbol": update.symbol, "price": update.price})

    async def book_sampler() -> None:
        while True:
            for token_id in token_ids:
                book = ws_feed.get_cached_book(token_id)
                if book is None:
                    continue
                sig = (
                    tuple((lvl.price, lvl.size) for lvl in book.bids),
                    tuple((lvl.price, lvl.size) for lvl in book.asks),
                )
                if last_book_sig.get(token_id) == sig:
                    continue
                last_book_sig[token_id] = sig
                await write({
                    "t": time.time(), "type": "poly_book", "token_id": token_id,
                    "bids": [[lvl.price, lvl.size] for lvl in book.bids],
                    "asks": [[lvl.price, lvl.size] for lvl in book.asks],
                })
            await asyncio.sleep(SAMPLE_INTERVAL_S)

    tasks = [
        asyncio.create_task(ws_feed.run(), name="ws"),
        asyncio.create_task(binance_consumer(), name="binance"),
        asyncio.create_task(book_sampler(), name="sampler"),
    ]
    try:
        await asyncio.sleep(args.hours * 3600.0)
    except asyncio.CancelledError:
        pass
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        f.close()
        await feed.aclose()
        n = sum(1 for _ in open(out_path, encoding="utf-8"))
        print(f"Done. {n} lines written to {out_path}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
