"""
Dump trades and equity_curve to CSV for external analysis.

Usage:
    python scripts/export_csv.py [--out-dir exports/]
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from storage.db import Database  # noqa: E402


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


async def export(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.DATABASE_PATH)
    await db.connect()

    trades = await db.get_all_trades()
    equity = await db.get_equity_curve()

    _write_csv(out_dir / "trades.csv", trades)
    _write_csv(out_dir / "equity_curve.csv", equity)

    print(f"Exported {len(trades)} trades -> {out_dir / 'trades.csv'}")
    print(f"Exported {len(equity)} equity rows -> {out_dir / 'equity_curve.csv'}")

    await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Export trade history and equity curve to CSV.")
    parser.add_argument("--out-dir", default="exports", help="Output directory (default: exports/)")
    args = parser.parse_args()
    asyncio.run(export(Path(args.out_dir)))


if __name__ == "__main__":
    main()
