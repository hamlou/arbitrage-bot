"""
Tests for:
1. storage/db.py — WAL journal mode is enabled when Database.connect() runs.
2. scripts/db_integrity_check.py — the one-shot PRAGMA integrity_check script.

All tests use temporary SQLite files; no network, no real database.
"""
import sqlite3

import scripts.db_integrity_check as integrity
from storage.db import Database


# -- WAL enablement -----------------------------------------------------------


async def test_connect_enables_wal_journal_mode(tmp_path):
    db_path = str(tmp_path / "wal.db")
    db = Database(db_path)
    await db.connect()

    # A separate connection observes the journal mode persisted on disk.
    conn = sqlite3.connect(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()

    await db.close()
    assert mode == "wal"


async def test_wal_survives_reconnect(tmp_path):
    """WAL is persistent in the DB file, so a fresh connect is still WAL."""
    db_path = str(tmp_path / "wal_persist.db")
    db = Database(db_path)
    await db.connect()
    await db.close()

    db2 = Database(db_path)
    await db2.connect()
    await db2.close()

    conn = sqlite3.connect(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode == "wal"


# -- integrity check: OK path --------------------------------------------------


async def test_log_signal_persists_book_imbalance(tmp_path):
    """Regression: the signals table must persist the measurement-only
    book_imbalance_pct field (2026-08-11) — the microstructure signal logged
    on every evaluation so the 100+ trade bar can test whether losing entries
    cluster on thin/one-sided books. Never gates anything; must survive the
    round-trip."""
    db_path = str(tmp_path / "imbalance.db")
    db = Database(db_path)
    await db.connect()
    await db.log_signal(
        market_id="m1", asset="BTC", implied_prob=0.5, polymarket_prob=0.51,
        edge_pct=1.0, confidence=0.9, fired=True,
        book_imbalance_pct=62.5,
    )
    signals = await db.get_signals(market_id="m1")
    await db.close()
    assert len(signals) == 1
    assert signals[0]["book_imbalance_pct"] == 62.5


async def test_healthy_database_prints_ok(tmp_path, capsys):
    db_path = str(tmp_path / "healthy.db")
    db = Database(db_path)
    await db.connect()
    await db.log_signal(
        market_id="m1", asset="BTC", implied_prob=0.5, polymarket_prob=0.51,
        edge_pct=1.0, confidence=0.9, fired=True,
    )
    await db.close()

    assert integrity.check_integrity(db_path) == "OK"

    exit_code = integrity.main(["--db", db_path])
    out, err = capsys.readouterr()
    assert exit_code == 0
    assert out.strip() == "OK"
    assert err == ""


# -- integrity check: problem paths --------------------------------------------


async def test_corrupt_database_reports_problem_text(tmp_path, capsys):
    db_path = tmp_path / "corrupt.db"
    db = Database(str(db_path))
    await db.connect()
    await db.log_signal(
        market_id="m1", asset="BTC", implied_prob=0.5, polymarket_prob=0.51,
        edge_pct=1.0, confidence=0.9, fired=True,
    )
    await db.close()

    # Corrupt the file deterministically: truncate it mid-file so later pages
    # (including the schema/freelist references) are gone. integrity_check then
    # reports "database disk image is malformed" instead of "ok".
    data = db_path.read_bytes()
    db_path.write_bytes(data[: len(data) // 2])

    result = integrity.check_integrity(str(db_path))
    assert result != "OK"  # actual problem text, not "OK"

    exit_code = integrity.main(["--db", str(db_path)])
    out, err = capsys.readouterr()
    assert exit_code == 1
    assert out.strip() == ""          # nothing printed on stdout
    assert err.strip() != ""          # the problem text goes to stderr
    assert "OK" not in err


def test_non_database_file_reports_exact_error(tmp_path, capsys):
    """A file that is not SQLite at all must surface the real error text."""
    db_path = tmp_path / "garbage.db"
    db_path.write_bytes(b"this is definitely not a sqlite database " * 20)

    result = integrity.check_integrity(str(db_path))
    assert result != "OK"

    exit_code = integrity.main(["--db", str(db_path)])
    out, err = capsys.readouterr()
    assert exit_code == 1
    assert out.strip() == ""
    assert err.strip() != ""


def test_missing_file_errors(tmp_path, capsys):
    exit_code = integrity.main(["--db", str(tmp_path / "does_not_exist.db")])
    out, err = capsys.readouterr()
    assert exit_code == 1
    assert "not found" in err
    assert out.strip() == ""


def test_default_db_path_is_used(tmp_path, monkeypatch, capsys):
    """main() falls back to settings.DATABASE_PATH when --db is omitted."""
    import asyncio

    target = tmp_path / "default.db"
    monkeypatch.setattr(integrity.settings, "DATABASE_PATH", str(target))

    db = Database(str(target))

    async def _prepare():
        await db.connect()
        await db.close()

    asyncio.run(_prepare())

    exit_code = integrity.main([])
    out, err = capsys.readouterr()
    assert exit_code == 0
    assert out.strip() == "OK"
