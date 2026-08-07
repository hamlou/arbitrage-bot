"""
Reports tick-to-signal and tick-to-order latency percentiles from recorded
latency_events, and compares them against a configurable assumed arbitrage
window so you can see, concretely, whether this bot is fast enough to matter.

Usage:
    python scripts/report_latency.py
    python scripts/report_latency.py --window-s 2.0

Window default: 2.0s (settings.ASSUMED_ARBITRAGE_WINDOW_S). This replaces the
old 2.7s guess — measured quote-response lags for Polymarket's short-duration
crypto up/down markets cluster ~347ms with exploitable dislocations up to ~2s
(OpenMarket 2026 dataset, arxiv.org/html/2607.26245v1). Polymarket's CLOB also
imposes a fixed 250ms taker-order delay (itode) on these markets, which is
added to the measured latency when judging the window.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from storage.db import Database  # noqa: E402
from scripts.check_clock_drift import drift_warning_line  # noqa: E402


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


async def report(window_s: float) -> None:
    db = Database(settings.DATABASE_PATH)
    await db.connect()
    events = await db.get_latency_events()
    await db.close()

    if not events:
        print("No latency events recorded yet. Run the bot for a while first.")
        return

    tick_to_signal = [e["tick_to_signal_ms"] for e in events if e["tick_to_signal_ms"] is not None]
    tick_to_order = [e["tick_to_order_ms"] for e in events if e["tick_to_order_ms"] is not None]
    fired_count = sum(1 for e in events if e["fired"])

    print("=" * 60)
    print("LATENCY REPORT")
    print("=" * 60)
    # Short timeout: this is a report, not a time audit — if the reference is
    # unreachable we'd rather print the report than hang up to 6s on NTP+HTTPS.
    warning = drift_warning_line(timeout=1.5)
    if warning:
        print(warning)
    print(f"Total evaluation cycles measured : {len(events)}")
    print(f"Cycles that fired a signal        : {fired_count}")
    print()
    print("Tick -> signal evaluated (ms):")
    print(f"  p50: {_percentile(tick_to_signal, 0.50):8.1f}   p95: {_percentile(tick_to_signal, 0.95):8.1f}   "
          f"p99: {_percentile(tick_to_signal, 0.99):8.1f}   max: {max(tick_to_signal, default=0):8.1f}")

    if tick_to_order:
        print()
        print("Tick -> order submitted (ms), fired cycles only:")
        print(f"  p50: {_percentile(tick_to_order, 0.50):8.1f}   p95: {_percentile(tick_to_order, 0.95):8.1f}   "
              f"p99: {_percentile(tick_to_order, 0.99):8.1f}   max: {max(tick_to_order, default=0):8.1f}")

        window_ms = window_s * 1000
        platform_delay_ms = settings.PLATFORM_TAKER_DELAY_MS
        p95 = _percentile(tick_to_order, 0.95)
        # Polymarket's CLOB holds marketable orders for a fixed 250ms
        # taker-delay window (itode) on fast up/down markets — that is ON TOP
        # of our measured tick->order latency, so it must be added before
        # comparing against the window.
        total_ms = p95 + platform_delay_ms
        print()
        print(f"Assumed arbitrage window: {window_s:.1f}s ({window_ms:.0f}ms)")
        print(f"  (window per OpenMarket 2026: ~347ms median lag, up to ~2s dislocations)")
        print(f"Measured p95 tick->order: {p95:.0f}ms")
        print(f"+ CLOB taker delay (itode, fixed): {platform_delay_ms:.0f}ms")
        print(f"= p95 end-to-end: {total_ms:.0f}ms")
        if total_ms < window_ms * 0.5:
            print(f"RESULT: comfortable — end-to-end p95 ({total_ms:.0f}ms) is well under half the window.")
        elif total_ms < window_ms:
            print(f"RESULT: tight — end-to-end p95 ({total_ms:.0f}ms) is under the window but with little margin.")
        else:
            print(f"RESULT: too slow — end-to-end p95 ({total_ms:.0f}ms) meets or exceeds the assumed window "
                  f"({window_ms:.0f}ms). Orders at this latency are likely reacting to a book that has "
                  f"already moved. Fix latency before trusting paper-mode expectancy numbers.")
    else:
        print("\nNo fired signals yet — no tick-to-order measurements to compare against the window.")
    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="Report latency percentiles vs. the arbitrage window.")
    parser.add_argument(
        "--window-s", type=float, default=settings.ASSUMED_ARBITRAGE_WINDOW_S,
        help=f"Assumed arbitrage window in seconds (default {settings.ASSUMED_ARBITRAGE_WINDOW_S}, "
             "per the OpenMarket 2026 measurement).",
    )
    args = parser.parse_args()
    asyncio.run(report(args.window_s))


if __name__ == "__main__":
    main()
