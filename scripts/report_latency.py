"""
Reports tick-to-signal and tick-to-order latency percentiles from recorded
latency_events, and compares them against a configurable assumed arbitrage
window so you can see, concretely, whether this bot is fast enough to matter.

Usage:
    python scripts/report_latency.py
    python scripts/report_latency.py --window-s 2.7
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
        p95 = _percentile(tick_to_order, 0.95)
        print()
        print(f"Assumed arbitrage window: {window_s:.1f}s ({window_ms:.0f}ms)")
        if p95 < window_ms * 0.5:
            print(f"RESULT: comfortable — p95 latency ({p95:.0f}ms) is well under half the window.")
        elif p95 < window_ms:
            print(f"RESULT: tight — p95 latency ({p95:.0f}ms) is under the window but with little margin.")
        else:
            print(f"RESULT: too slow — p95 latency ({p95:.0f}ms) meets or exceeds the assumed window "
                  f"({window_ms:.0f}ms). Orders at this latency are likely reacting to a book that has "
                  f"already moved. Fix latency before trusting paper-mode expectancy numbers.")
    else:
        print("\nNo fired signals yet — no tick-to-order measurements to compare against the window.")
    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="Report latency percentiles vs. the arbitrage window.")
    parser.add_argument(
        "--window-s", type=float, default=2.7,
        help="Assumed arbitrage window in seconds (default 2.7, per the most recent reporting "
             "referenced when this project was built — verify this number is still current before "
             "trusting the comparison).",
    )
    args = parser.parse_args()
    asyncio.run(report(args.window_s))


if __name__ == "__main__":
    main()
