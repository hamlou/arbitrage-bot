"""
Replay backtest — measure the arbitrage window empirically from a capture.

Consumes a capture from scripts/capture_market_data.py and answers the
question that paper mode can't: "if we had executed at +100ms / +300ms /
+600ms / +2000ms after a Binance move, how much of the mispricing would
still have been there — and how often had the market already repriced?"

For every Binance move >= --move-pct, it records the direction-implied
Polymarket token's mid at decision time, then the mid known at each execution
offset. A move counts as "already repriced" at an offset when the mid has
moved by >= --reprice-move (default 0.005) from its decision value.

Usage:
    python scripts/capture_market_data.py --hours 1
    python scripts/replay_arb.py storage/captures/capture_<ts>.jsonl
    python scripts/replay_arb.py capture.jsonl --move-pct 0.0010 --offsets 100,300,600,2000

Output: per-speed table — % of moves the market had already repriced (edge
gone) and the median remaining mispricing (|mid - decision_mid|), plus the
measured repricing-lag distribution. Honest input to the question "is our
execution speed fast enough for this market?" — measured, not guessed.
"""
from __future__ import annotations

import argparse
import bisect
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Optional

DEFAULT_OFFSETS_MS = [100, 300, 600, 2000]


def load_capture(path: Path) -> dict:
    markets: dict[str, dict] = {}        # token_id -> {asset, side(YES/NO)}
    binance: list[dict] = []             # {t, symbol, price}
    books: dict[str, list[tuple[float, float, float]]] = defaultdict(list)  # token -> [(t, bids, asks)]
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec["type"] == "market":
            markets[rec["token_yes"]] = {"asset": rec["asset"], "side": "YES"}
            markets[rec["token_no"]] = {"asset": rec["asset"], "side": "NO"}
        elif rec["type"] == "binance":
            binance.append({"t": rec["t"], "symbol": rec["symbol"], "price": rec["price"]})
        elif rec["type"] == "poly_book":
            mid = 0.0
            if rec["bids"] and rec["asks"]:
                mid = (rec["bids"][0][0] + rec["asks"][0][0]) / 2.0
            books[rec["token_id"]].append((rec["t"], mid))
    return {"markets": markets, "binance": binance, "books": books}


def mid_at_or_before(series: list[tuple[float, float]], t: float) -> Optional[float]:
    """Latest mid known at or before time t (None if none yet)."""
    if not series:
        return None
    i = bisect.bisect_right(series, (t, float("inf"))) - 1
    return series[i][1] if i >= 0 else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", help="path to the .jsonl capture file")
    parser.add_argument("--move-pct", type=float, default=0.0010, help="Binance move size that counts")
    parser.add_argument("--reprice-move", type=float, default=0.005, help="mid move that counts as 'repriced'")
    parser.add_argument("--offsets", default=",".join(map(str, DEFAULT_OFFSETS_MS)), help="execution offsets in ms")
    args = parser.parse_args()
    offsets_ms = [int(x) for x in args.offsets.split(",") if x.strip()]
    offsets_s = [o / 1000.0 for o in offsets_ms]

    cap = load_capture(Path(args.capture))
    markets, binance, books = cap["markets"], cap["binance"], cap["books"]
    if not binance or not books:
        print("Capture is empty or missing Binance/Polymarket data — record for longer.")
        raise SystemExit(1)

    # Which token each symbol's move implies (YES rises on an UP move).
    def implied_token(symbol: str, direction: str) -> Optional[str]:
        asset = symbol[:-4] if symbol.endswith("USDT") else symbol
        for token_id, meta in markets.items():
            if meta["asset"] == asset and meta["side"] == direction:
                return token_id
        return None

    # Identify moves: |tick-to-tick| >= move_pct.
    moves: list[dict] = []
    prev: dict[str, float] = {}
    for t in binance:
        last = prev.get(t["symbol"])
        prev[t["symbol"]] = t["price"]
        if last is None or last <= 0:
            continue
        move_pct = abs(t["price"] - last) / last
        if move_pct < args.move_pct:
            continue
        direction = "YES" if t["price"] > last else "NO"
        token_id = implied_token(t["symbol"], direction)
        if token_id is None:
            continue
        baseline = mid_at_or_before(books.get(token_id, []), t["t"])
        if baseline is None:
            continue
        moves.append({"t": t["t"], "symbol": t["symbol"], "move_pct": move_pct,
                      "token_id": token_id, "baseline": baseline})

    if not moves:
        print(f"No Binance moves >= {args.move_pct:.4%} with a book to compare — "
              "try a longer capture or a lower --move-pct.")
        raise SystemExit(1)

    # Per-offset aggregation.
    offsets = {o_ms: {"repriced": 0, "dislocation": [], "lags_ms": []} for o_ms in offsets_ms}
    all_lags_ms: list[float] = []
    timed_out = 0
    for m in moves:
        series = books.get(m["token_id"], [])
        repriced_at: Optional[float] = None
        for o_ms, o_s in zip(offsets_ms, offsets_s):
            mid = mid_at_or_before(series, m["t"] + o_s)
            if mid is None:
                continue
            drift = abs(mid - m["baseline"])
            offsets[o_ms]["dislocation"].append(drift)
            if drift >= args.reprice_move:
                offsets[o_ms]["repriced"] += 1
                if repriced_at is None:
                    repriced_at = o_ms
        if repriced_at is None:
            timed_out += 1
        else:
            all_lags_ms.append(repriced_at)

    n = len(moves)
    print(f"Moves analyzed: {n} (Binance move >= {args.move_pct:.4%}, "
          f"reprice = mid move >= {args.reprice_move:.3f})")
    print(f"Capture: {Path(args.capture).name}")
    print()
    print(f"{'execution speed':>16}{'already repriced':>18}{'median |mid drift|':>20}")
    print("-" * 56)
    for o_ms in offsets_ms:
        repriced_pct = offsets[o_ms]["repriced"] / n * 100.0
        med_drift = statistics.median(offsets[o_ms]["dislocation"]) if offsets[o_ms]["dislocation"] else float("nan")
        print(f"{'+' + str(o_ms) + 'ms':>16}{repriced_pct:>15.1f}%{med_drift:>19.3f}")
    print()
    print(f"Markets that never repriced within {offsets_ms[-1]}ms: {timed_out}/{n} "
          f"({timed_out / n * 100:.1f}%)")
    if all_lags_ms:
        # statistics.quantiles(n=10) needs >= 10 values — short captures
        # (exactly the first-run case) would crash without the guard.
        if len(all_lags_ms) >= 10:
            p90 = f"{statistics.quantiles(all_lags_ms, n=10)[8]:.0f}ms"
        else:
            p90 = f"n/a ({len(all_lags_ms)} samples)"
        print(f"First-reprice lag: median {statistics.median(all_lags_ms):.0f}ms, p90 {p90}")
    print()
    print("Read it as: at +300ms execution, if the market has already repriced "
          "60% of moves, the remaining edge is only the 40% you get to first — "
          "that number is your real winnable window.")


if __name__ == "__main__":
    main()
