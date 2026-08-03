"""
Measures the difference between the local system clock and a public time
reference, in milliseconds.

Uses NTP first (ntplib against a public pool server — millisecond precision).
If ntplib is missing or NTP is blocked/unreachable, falls back to an HTTPS
time endpoint (Cloudflare's cdn-cgi/trace, which exposes a millisecond-
precision epoch timestamp with no API key).

The signed drift is printed, and a warning is raised when the absolute drift
exceeds a threshold (default 50 ms) so that latency-sensitive conclusions
know the local clock may be lying.

Usage:
    python scripts/check_clock_drift.py
    python scripts/check_clock_drift.py --threshold-ms 100 --timeout 2
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

NTP_SERVER = "pool.ntp.org"
HTTPS_TIME_URL = "https://www.cloudflare.com/cdn-cgi/trace"
DEFAULT_THRESHOLD_MS = 50.0


def _ntp_drift_ms(timeout: float) -> tuple[float | None, str]:
    try:
        import ntplib
    except ImportError:
        return None, "ntplib-not-installed"
    try:
        response = ntplib.NTPClient().request(NTP_SERVER, version=3, timeout=timeout)
        # ntplib offset = (local clock - server clock), seconds. Positive = ahead.
        return response.offset * 1000.0, f"ntp:{NTP_SERVER}"
    except (OSError, ntplib.NTPException) as exc:  # blocked/timeout/bad reply -> HTTPS
        return None, f"ntp-unreachable ({exc})"


def _https_drift_ms(timeout: float) -> tuple[float | None, str]:
    try:
        t0 = time.time()
        response = httpx.get(HTTPS_TIME_URL, timeout=timeout)
        t1 = time.time()
        response.raise_for_status()
    except (httpx.HTTPError, OSError) as exc:
        return None, f"https-unreachable ({exc})"
    for line in response.text.splitlines():
        if line.startswith("ts="):
            server_ts = float(line.split("=", 1)[1])
            rtt = t1 - t0
            # Reference is the server timestamp corrected by half the round trip;
            # positive drift = local clock ahead of the reference.
            drift_ms = (t1 - (server_ts + rtt / 2.0)) * 1000.0
            return drift_ms, "https:cloudflare.com/cdn-cgi/trace"
    return None, "https-no-timestamp"


def measure_drift_ms(timeout: float = 3.0) -> tuple[float | None, str]:
    """Return (drift_ms, source). Positive drift = system clock ahead of the
    reference. Returns (None, reason) if no time source could be reached."""
    drift, source = _ntp_drift_ms(timeout)
    if drift is not None:
        return drift, source
    return _https_drift_ms(timeout)


def drift_warning_line(
    max_drift_ms: float = DEFAULT_THRESHOLD_MS,
    timeout: float = 3.0,
    drift_ms: float | None = None,
    source: str = "",
) -> str | None:
    """One-line warning when |drift| exceeds max_drift_ms, else None.

    Used by report scripts (report_latency.py, validate_paper_run.py) so a
    badly skewed system clock is called out before any latency conclusions.
    """
    if drift_ms is None:
        drift_ms, source = measure_drift_ms(timeout=timeout)
    if drift_ms is None or abs(drift_ms) <= max_drift_ms:
        return None
    return (
        f"WARNING: system clock is {abs(drift_ms):.1f} ms off the reference "
        f"({source}) - exceeds {max_drift_ms:.0f} ms - latency numbers may not "
        "be trustworthy."
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure local clock drift vs a public time source."
    )
    parser.add_argument("--threshold-ms", type=float, default=DEFAULT_THRESHOLD_MS)
    parser.add_argument("--timeout", type=float, default=3.0)
    args = parser.parse_args()

    drift_ms, source = measure_drift_ms(timeout=args.timeout)
    if drift_ms is None:
        print(
            f"Could not measure clock drift — no time source reachable ({source}).",
            file=sys.stderr,
        )
        return 1

    direction = "ahead of" if drift_ms >= 0 else "behind"
    print(
        f"Clock drift vs {source}: {drift_ms:+.1f} ms "
        f"(system clock is {abs(drift_ms):.1f} ms {direction} the reference)"
    )
    warning = drift_warning_line(
        max_drift_ms=args.threshold_ms, drift_ms=drift_ms, source=source
    )
    if warning:
        print(warning)
    return 0


if __name__ == "__main__":
    sys.exit(main())
