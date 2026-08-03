"""
Fits the momentum -> continuation-probability calibration curve from
historical price data, replacing the hand-picked `sensitivity=8.0` constant
that shipped in the original scaffold with something actually derived from
real BTC/ETH price behavior.

This only needs historical PRICE data (not Polymarket data) — the question
being calibrated is "given a move of this size over this lookback window, how
often does the direction hold by the horizon," which is a property of the
asset's own price process.

Input format: a CSV with columns `timestamp,price` (unix seconds, float).
For raw Binance klines CSVs (the standard 12-column format from
data.binance.vision), use --binance-klines-csv instead — this script maps
that layout to timestamp/price automatically using each candle's close.

Usage:
    python scripts/calibrate_momentum_model.py --price-csv btc_1min.csv --asset BTC --horizon-minutes 15
    python scripts/calibrate_momentum_model.py --binance-klines-csv BTCUSDT-1m-2026-06.csv --asset BTC --horizon-minutes 5
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.calibration import (  # noqa: E402
    DEFAULT_CALIBRATION_PATH,
    build_samples_from_price_series,
    fit_calibration,
    load_calibration,
    save_calibration,
)
from engine.signal import MOMENTUM_LOOKBACK_S  # noqa: E402


def load_price_csv(path: Path) -> tuple[list[float], list[float]]:
    timestamps, prices = [], []
    with path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            timestamps.append(float(row["timestamp"]))
            prices.append(float(row["price"]))
    return timestamps, prices


def load_binance_klines_csv(path: Path) -> tuple[list[float], list[float]]:
    """
    Standard Binance klines CSV layout (data.binance.vision), 12 columns,
    no header row:
      open_time, open, high, low, close, volume, close_time, quote_volume,
      trades, taker_buy_base, taker_buy_quote, ignore
    open_time is in MILLISECONDS.
    """
    timestamps, prices = [], []
    with path.open() as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or not row[0].replace(".", "", 1).isdigit():
                continue  # skip any header/blank lines defensively
            open_time_ms = float(row[0])
            close_price = float(row[4])
            timestamps.append(open_time_ms / 1000.0)
            prices.append(close_price)
    return timestamps, prices


def main() -> None:
    parser = argparse.ArgumentParser(description="Fit the momentum calibration curve from historical price data.")
    parser.add_argument("--price-csv", type=str, default="", help="CSV with columns: timestamp,price")
    parser.add_argument("--binance-klines-csv", type=str, default="", help="Raw Binance klines CSV (data.binance.vision format)")
    parser.add_argument("--horizon-minutes", type=int, default=15, choices=[5, 15])
    parser.add_argument("--lookback-s", type=float, default=MOMENTUM_LOOKBACK_S,
                         help=f"Momentum lookback window in seconds (default matches "
                              f"engine/signal.py's live tracker: {MOMENTUM_LOOKBACK_S}s)")
    parser.add_argument("--n-bins", type=int, default=8)
    parser.add_argument("--output", type=str, default=str(DEFAULT_CALIBRATION_PATH))
    args = parser.parse_args()

    if not args.price_csv and not args.binance_klines_csv:
        print("Provide either --price-csv or --binance-klines-csv. See this script's docstring "
              "for the expected format of each.")
        sys.exit(1)

    if args.binance_klines_csv:
        timestamps, prices = load_binance_klines_csv(Path(args.binance_klines_csv))
    else:
        timestamps, prices = load_price_csv(Path(args.price_csv))

    if len(timestamps) < 100:
        print(f"Only {len(timestamps)} price points loaded — that's too little to fit a "
              f"meaningful calibration curve. Pull a wider historical window and try again.")
        sys.exit(1)

    horizon_s = args.horizon_minutes * 60
    print(f"Loaded {len(prices)} price points spanning "
          f"{(timestamps[-1] - timestamps[0]) / 86400:.1f} days.")
    print(f"Building samples: lookback={args.lookback_s:.0f}s, horizon={horizon_s:.0f}s "
          f"({args.horizon_minutes}min contracts)...")

    samples = build_samples_from_price_series(
        timestamps, prices, lookback_s=args.lookback_s, horizon_s=horizon_s,
    )
    print(f"Built {len(samples)} (momentum, outcome) samples.")

    if len(samples) < len(timestamps) * 0.1:
        avg_spacing_s = (timestamps[-1] - timestamps[0]) / max(len(timestamps) - 1, 1)
        print(
            f"\nWARNING: that's a very low sample yield ({len(samples)} from {len(timestamps)} "
            f"price points). This usually means --lookback-s ({args.lookback_s:.0f}s) is finer "
            f"than your data's own spacing (~{avg_spacing_s:.0f}s between points) — e.g. a 30s "
            f"lookback can't find an earlier point inside 1-minute klines. Try a --lookback-s "
            f"that's at least 2-3x your data's spacing, or use finer-grained (e.g. 1-second) "
            f"historical data if you have access to it."
        )

    model = fit_calibration(samples, horizon_minutes=args.horizon_minutes, n_bins=args.n_bins)
    if model is None:
        print(f"Not enough samples to fit {args.n_bins} bins (need at least {args.n_bins * 5}). "
              f"Use a longer historical window, more bins, or fewer bins.")
        sys.exit(1)

    print("\nFitted calibration curve:")
    print(f"{'magnitude':>12}  {'P(direction holds)':>20}")
    for mag, prob in zip(model.magnitude_breakpoints, model.continuation_probability):
        print(f"{mag:12.5f}  {prob:20.3f}")

    out_path = Path(args.output)
    existing = load_calibration(out_path)
    existing[args.horizon_minutes] = model
    save_calibration(existing, out_path)
    print(f"\nSaved calibration for the {args.horizon_minutes}-minute horizon to {out_path}")
    print("engine/signal.py will pick this up automatically on next startup — no code change needed.")
    print("\nRe-run this periodically as more historical data becomes available; a calibration "
          "fit on last month's volatility regime won't necessarily hold for next month's.")


if __name__ == "__main__":
    main()
