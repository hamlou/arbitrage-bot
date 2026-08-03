"""
Reads SQLite paper-trade history and reports whether it meets the acceptance
criteria for even considering Prompt 6's live-trading gate. Run this BEFORE
touching any LIVE_TRADING_CONFIRMED_* flag.

Usage:
    python scripts/validate_paper_run.py
    python scripts/validate_paper_run.py --min-trades 300 --min-win-rate 0.75
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from storage.db import Database  # noqa: E402
from scripts.check_clock_drift import drift_warning_line  # noqa: E402

DEFAULT_MIN_TRADES = 200
DEFAULT_MIN_DAYS = 7
DEFAULT_MIN_WIN_RATE = 0.70
DEFAULT_MIN_DISTINCT_DAYS = 5          # separate from min_days: calendar span vs. actual trading days
DEFAULT_MAX_SINGLE_DAY_SHARE = 0.50    # warn if one day accounts for more than this fraction of all trades


@dataclass
class ValidationResult:
    total_trades: int
    win_rate: float
    expectancy_usd: float
    max_drawdown_pct: float
    days_elapsed: float
    passed: bool
    failures: list[str]
    warnings: list[str] = field(default_factory=list)
    distinct_trading_days: int = 0
    max_single_day_share: float = 0.0


def _compute_max_drawdown_pct(equity_rows: list[dict]) -> float:
    if not equity_rows:
        return 0.0
    peak = equity_rows[0]["balance_usd"]
    max_dd = 0.0
    for row in equity_rows:
        bal = row["balance_usd"]
        peak = max(peak, bal)
        if peak > 0:
            dd = (peak - bal) / peak
            max_dd = max(max_dd, dd)
    return max_dd


async def validate(
    min_trades: int,
    min_days: float,
    min_win_rate: float,
    kill_threshold_pct: float,
    min_distinct_days: int = DEFAULT_MIN_DISTINCT_DAYS,
    max_single_day_share: float = DEFAULT_MAX_SINGLE_DAY_SHARE,
) -> ValidationResult:
    db = Database(settings.DATABASE_PATH)
    await db.connect()

    trades = await db.get_all_trades(mode="PAPER")
    equity = await db.get_equity_curve(mode="PAPER")
    await db.close()

    closed = [t for t in trades if t["status"] == "CLOSED" and t["realized_pnl_usd"] is not None]
    total_trades = len(closed)

    wins = [t for t in closed if t["realized_pnl_usd"] > 0]
    win_rate = (len(wins) / total_trades) if total_trades else 0.0

    # Expectancy per trade AFTER modeled fees and slippage: realized_pnl_usd
    # already has fees deducted (see broker_paper.py), and entry_price already
    # reflects simulated slippage from walking the real order book — so a
    # simple mean of realized_pnl_usd is expectancy net of both.
    expectancy_usd = (sum(t["realized_pnl_usd"] for t in closed) / total_trades) if total_trades else 0.0

    max_drawdown_pct = _compute_max_drawdown_pct(equity)

    if closed:
        first_ts = min(t["entry_ts"] for t in trades)
        days_elapsed = (datetime.now(timezone.utc).timestamp() - first_ts) / 86400
    else:
        days_elapsed = 0.0

    # -- Regime coverage: a PASS built on one calm (or one wild) day tells you
    # less than the same trade count spread across genuinely different market
    # conditions. This is a warning, not a hard failure — there's no principled
    # threshold for "enough regime diversity," just a heads-up to look closer.
    day_counts = Counter(
        datetime.fromtimestamp(t["entry_ts"], tz=timezone.utc).strftime("%Y-%m-%d") for t in closed
    )
    distinct_trading_days = len(day_counts)
    max_single_day_share = (max(day_counts.values()) / total_trades) if total_trades else 0.0

    failures = []
    if total_trades < min_trades:
        failures.append(f"trades {total_trades} < required {min_trades}")
    if days_elapsed < min_days:
        failures.append(f"days elapsed {days_elapsed:.1f} < required {min_days}")
    if win_rate < min_win_rate:
        failures.append(f"win rate {win_rate:.1%} < required {min_win_rate:.1%}")
    if expectancy_usd <= 0:
        failures.append(f"expectancy ${expectancy_usd:.4f} is not positive (after fees & slippage)")
    if max_drawdown_pct >= kill_threshold_pct:
        failures.append(f"max drawdown {max_drawdown_pct:.1%} >= kill threshold {kill_threshold_pct:.1%}")

    warnings = []
    if closed and distinct_trading_days < min_distinct_days:
        warnings.append(
            f"only {distinct_trading_days} distinct trading day(s) — a PASS here mostly reflects "
            f"one market regime, not sustained performance across different conditions"
        )
    if closed and max_single_day_share > DEFAULT_MAX_SINGLE_DAY_SHARE:
        warnings.append(
            f"{max_single_day_share:.0%} of all trades happened on a single day — "
            f"check that day wasn't an outlier (unusual volatility) skewing the whole result"
        )

    return ValidationResult(
        total_trades=total_trades,
        win_rate=win_rate,
        expectancy_usd=expectancy_usd,
        max_drawdown_pct=max_drawdown_pct,
        days_elapsed=days_elapsed,
        passed=not failures,
        failures=failures,
        warnings=warnings,
        distinct_trading_days=distinct_trading_days,
        max_single_day_share=max_single_day_share,
    )


def _print_report(r: ValidationResult) -> None:
    print("=" * 60)
    print("PAPER TRADING VALIDATION REPORT")
    print("=" * 60)
    # Short timeout: this is a report, not a time audit — if the reference is
    # unreachable we'd rather print the report than hang up to 6s on NTP+HTTPS.
    warning = drift_warning_line(timeout=1.5)
    if warning:
        print(warning)
    print(f"Completed paper trades : {r.total_trades}")
    print(f"Win rate                : {r.win_rate:.1%}")
    print(f"Expectancy / trade      : ${r.expectancy_usd:.4f}  (net of fees & slippage)")
    print(f"Max drawdown observed   : {r.max_drawdown_pct:.1%}")
    print(f"Days since first trade  : {r.days_elapsed:.1f}")
    print(f"Distinct trading days   : {r.distinct_trading_days}")
    print(f"Busiest single day share: {r.max_single_day_share:.0%} of all trades")
    print("-" * 60)
    if r.passed:
        print("RESULT: PASS")
    else:
        print("RESULT: FAIL")
        for f in r.failures:
            print(f"  - {f}")
    if r.warnings:
        print("-" * 60)
        print("WARNINGS (not blocking, but worth a look):")
        for w in r.warnings:
            print(f"  - {w}")
    print("=" * 60)
    if not r.passed:
        print(
            "\nThis is the gate for even considering Prompt 6's live-trading flags. "
            "A FAIL here means: keep paper trading, don't touch "
            "LIVE_TRADING_CONFIRMED_1/2/3 yet."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a paper-trading run against acceptance criteria.")
    parser.add_argument("--min-trades", type=int, default=DEFAULT_MIN_TRADES)
    parser.add_argument("--min-days", type=float, default=DEFAULT_MIN_DAYS)
    parser.add_argument("--min-win-rate", type=float, default=DEFAULT_MIN_WIN_RATE)
    parser.add_argument("--min-distinct-days", type=int, default=DEFAULT_MIN_DISTINCT_DAYS)
    args = parser.parse_args()

    result = asyncio.run(
        validate(
            min_trades=args.min_trades,
            min_days=args.min_days,
            min_win_rate=args.min_win_rate,
            kill_threshold_pct=settings.TOTAL_DRAWDOWN_KILL_PCT,
            min_distinct_days=args.min_distinct_days,
        )
    )
    _print_report(result)
    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
