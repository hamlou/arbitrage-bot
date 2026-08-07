"""
Timing-gap diagnostic: does this bot fit inside the real Polymarket arbitrage
window?

Answers, with actual numbers instead of vibes:

    1. What is the realistic window? Measured quote-response lags for
       Polymarket's short-duration crypto up/down markets cluster around a
       347ms median, with exploitable dislocations up to ~2s — see the
       OpenMarket 2026 dataset (arxiv.org/html/2607.26245v1, "A Synchronized
       Polymarket-Binance Dataset for High-Frequency Prediction-Market
       Research"). This replaces the old 2.7s guess that report_latency.py
       used to default to, which came from a secondhand article, not a
       measurement.

    2. What does the bot's path actually cost? Measured from the DB's
       latency_events (tick received -> signal evaluated -> order submitted),
       plus the components that measurements can't see: the network legs, the
       exchange round trips, and Polymarket's platform-imposed 250ms
       taker-order delay (the itode market flag, per
       docs.polymarket.com/concepts/order-lifecycle — marketable orders are
       held for a 250ms window before matching).

    3. Verdict: comfortable / tight / too slow, printed as a RESULT line.

Usage:
    python scripts/diagnose_timing.py
    python scripts/diagnose_timing.py --window-s 1.5

Reads only the local DB; no network calls.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from scripts.check_clock_drift import drift_warning_line  # noqa: E402
from storage.db import Database  # noqa: E402

# Structural budget components the latency_events table can't measure: our own
# polling cadence + the CLOB's order lifecycle. These are deliberately
# conservative assumptions; the table holds the measured part.
BINANCE_WS_LATENCY_MS = 50.0      # public WS tick delivery, typical
SIGNAL_TO_ORDER_OVERHEAD_MS = 30.0  # sizing + place_order round trip
CLOB_MATCH_ROUND_TRIP_MS = 50.0    # order -> match -> ack


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _verdict(total_ms: float, window_ms: float) -> str:
    if total_ms < window_ms * 0.5:
        return "comfortable"
    if total_ms < window_ms:
        return "tight"
    return "too slow"


async def diagnose(window_s: float) -> None:
    db = Database(settings.DATABASE_PATH)
    await db.connect()
    events = await db.get_latency_events()
    await db.close()

    print("=" * 60)
    print("TIMING-GAP DIAGNOSTIC")
    print("=" * 60)
    warning = drift_warning_line(timeout=1.5)
    if warning:
        print(warning)
    print()
    print("Reference window (measured, not guessed):")
    print("  Polymarket crypto up/down markets lag the underlying exchange")
    print("  by ~347ms median, with exploitable dislocations up to ~2s.")
    print("  Source: OpenMarket 2026 dataset (arxiv.org/html/2607.26245v1).")
    print(f"  Using window: {window_s:.1f}s ({window_s * 1000:.0f}ms)")
    print()
    print("Platform-imposed cost (can't be engineered around):")
    print(f"  CLOB 250ms taker-order delay (itode) on fast up/down markets.")
    print(f"  Source: docs.polymarket.com/concepts/order-lifecycle")
    print()

    window_ms = window_s * 1000
    platform_delay_ms = settings.PLATFORM_TAKER_DELAY_MS

    if not events:
        print("No latency_events recorded yet — run the bot for a while first.")
        print()
        # Even without measurements, show the structural budget so the answer
        # isn't a shrug.
        structural_total = (
            BINANCE_WS_LATENCY_MS
            + SIGNAL_TO_ORDER_OVERHEAD_MS
            + CLOB_MATCH_ROUND_TRIP_MS
            + platform_delay_ms
        )
        print("Structural budget (no measurements yet — assumptions only):")
        print(f"  Binance WS delivery (assumed) ....... {BINANCE_WS_LATENCY_MS:6.0f} ms")
        print(f"  Signal->order overhead (assumed) .... {SIGNAL_TO_ORDER_OVERHEAD_MS:6.0f} ms")
        print(f"  CLOB match round trip (assumed) ..... {CLOB_MATCH_ROUND_TRIP_MS:6.0f} ms")
        print(f"  CLOB taker delay (itode, fixed) ..... {platform_delay_ms:6.0f} ms")
        print(f"  TOTAL ............................... {structural_total:6.0f} ms")
        verdict = _verdict(structural_total, window_ms)
        print()
        print(f"RESULT: {verdict} — structural budget {structural_total:.0f}ms vs "
              f"window {window_ms:.0f}ms (measurements pending).")
        print("=" * 60)
        return

    tick_to_signal = [e["tick_to_signal_ms"] for e in events if e["tick_to_signal_ms"] is not None]
    tick_to_order = [e["tick_to_order_ms"] for e in events if e["tick_to_order_ms"] is not None]
    fired = sum(1 for e in events if e["fired"])

    print(f"Measured cycles: {len(events)}  (fired signals: {fired})")
    print()
    print("Measured (from latency_events):")
    if tick_to_signal:
        print(f"  Tick->signal   p50 {_percentile(tick_to_signal, 0.50):6.0f} ms   "
              f"p95 {_percentile(tick_to_signal, 0.95):6.0f} ms")
    if tick_to_order:
        p95_order = _percentile(tick_to_order, 0.95)
        print(f"  Tick->order    p50 {_percentile(tick_to_order, 0.50):6.0f} ms   "
              f"p95 {p95_order:6.0f} ms")
        print()
        print("Full budget (measured + fixed costs):")
        print(f"  Tick->order p95 (measured) .......... {p95_order:6.0f} ms")
        print(f"  CLOB taker delay (itode, fixed) ..... {platform_delay_ms:6.0f} ms")
        total = p95_order + platform_delay_ms
        print(f"  TOTAL ............................... {total:6.0f} ms")
        verdict = _verdict(total, window_ms)
        print()
        print(f"RESULT: {verdict} — p95 end-to-end {total:.0f}ms vs window "
              f"{window_ms:.0f}ms.")
        if verdict == "too slow":
            print("  The 250ms CLOB delay alone eats 12.5% of a 2s window; if the")
            print("  measured part is also large, this edge cannot be won on raw")
            print("  speed alone — it needs the slower, structural edges (sum-to-one")
            print("  or mispricing that persists for seconds, not milliseconds).")
        elif verdict == "tight":
            print("  Orders land inside the window but with little margin; every")
            print("  millisecond of jitter counts. Check clock drift and colocate.")
    else:
        print("  No fired signals yet — no tick->order measurements to compare.")
        print(f"  (Structural-only budget: ~{BINANCE_WS_LATENCY_MS + SIGNAL_TO_ORDER_OVERHEAD_MS + CLOB_MATCH_ROUND_TRIP_MS + platform_delay_ms:.0f}ms)")
    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose whether the bot fits the Polymarket timing gap.")
    parser.add_argument(
        "--window-s",
        type=float,
        default=settings.ASSUMED_ARBITRAGE_WINDOW_S,
        help=f"Assumed arbitrage window in seconds (default {settings.ASSUMED_ARBITRAGE_WINDOW_S}, "
             "per the OpenMarket 2026 measurement).",
    )
    args = parser.parse_args()
    asyncio.run(diagnose(args.window_s))


if __name__ == "__main__":
    main()
