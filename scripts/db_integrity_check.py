"""
One-shot SQLite integrity check for the bot's database.

Usage:
    python scripts/db_integrity_check.py
    python scripts/db_integrity_check.py --db path/to/arb_bot.db

Connects to the database and runs `PRAGMA integrity_check`. Prints "OK" when
the database passes, or the actual problem text when it does not (including
the exact error when the file is not even a valid SQLite database). Exits 0
on a clean database, 1 on any problem.

NOTE: this deliberately uses the sync sqlite3 module rather than the async
storage.db.Database class — Database has no method for arbitrary PRAGMA
queries, and a one-shot diagnostic doesn't need the async machinery.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# Allow running as `python scripts/db_integrity_check.py` from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def check_integrity(db_path: str) -> str:
    """
    Run PRAGMA integrity_check against the database at `db_path`.

    Returns "OK" when every returned row is "ok", otherwise the actual problem
    text (all problem rows joined, or the exact sqlite error message when the
    file cannot be read as a database at all).
    """
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as exc:
        return str(exc)
    finally:
        conn.close()

    problems = [row[0] for row in rows if row[0] != "ok"]
    if not problems:
        return "OK"
    return "\n".join(problems)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check the bot's SQLite database integrity via PRAGMA integrity_check."
    )
    parser.add_argument(
        "--db",
        default=settings.DATABASE_PATH,
        help="Path to the database file (default: settings.DATABASE_PATH).",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = REPO_ROOT / db_path
    if not db_path.is_file():
        print(f"ERROR: database file not found: {db_path}", file=sys.stderr)
        return 1

    result = check_integrity(str(db_path))
    if result == "OK":
        print("OK")
        return 0

    print(result, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
