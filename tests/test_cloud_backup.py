"""Tests for engine/cloud_backup.py — Telegram-based ledger persistence.

No real network: all Telegram calls go through a scripted fake httpx client.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from engine import cloud_backup


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, json: dict | None = None, content: bytes = b"", status: int = 200):
        self._json = json if json is not None else {}
        self.content = content
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._json


class FakeClient:
    """Scripted httpx.AsyncClient stand-in; matches responses by URL substring."""

    def __init__(self, responses: list[tuple[str, FakeResponse]] | None = None):
        self.responses = responses or []
        self.calls: list[tuple[str, str, dict]] = []

    async def post(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append(("post", url, kwargs))
        return self._pick(url)

    async def get(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append(("get", url, kwargs))
        return self._pick(url)

    async def aclose(self) -> None:
        pass

    def _pick(self, url: str) -> FakeResponse:
        for needle, resp in self.responses:
            if needle in url:
                return resp
        return FakeResponse({})


def _make_db_bytes() -> bytes:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "seed.db"
        con = sqlite3.connect(str(p))
        con.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY)")
        con.execute("INSERT INTO trades VALUES (1)")
        con.commit()
        con.close()
        return p.read_bytes()


def _make_ledger(path: Path) -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY)")
    con.execute("INSERT INTO trades VALUES (7)")
    con.commit()
    con.close()


def _doc_update(file_id: str, fname: str = cloud_backup.BACKUP_FILENAME) -> dict:
    return {
        "update_id": 1,
        "message": {
            "document": {"file_id": file_id, "file_name": fname},
        },
    }


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def test_db_missing_or_empty(tmp_path):
    missing = tmp_path / "nope.db"
    assert cloud_backup.db_missing_or_empty(missing) is True

    empty = tmp_path / "empty.db"
    sqlite3.connect(str(empty)).close()  # creates a zero-table file
    assert cloud_backup.db_missing_or_empty(empty) is True

    full = tmp_path / "full.db"
    _make_ledger(full)
    assert cloud_backup.db_missing_or_empty(full) is False


def test_make_snapshot_preserves_data(tmp_path):
    src = tmp_path / "src.db"
    _make_ledger(src)
    dst = tmp_path / "snap.db"
    cloud_backup.make_snapshot(src, dst)
    con = sqlite3.connect(str(dst))
    assert con.execute("SELECT id FROM trades").fetchone()[0] == 7
    con.close()


def test_backup_enabled():
    class S:
        CLOUD_BACKUP_ENABLED = True
        TELEGRAM_BOT_TOKEN = "tok"
        TELEGRAM_CHAT_ID = "123"

    class S_off(S):
        CLOUD_BACKUP_ENABLED = False

    class S_no_tg(S):
        TELEGRAM_BOT_TOKEN = None

    assert cloud_backup.backup_enabled(S()) is True
    assert cloud_backup.backup_enabled(S_off()) is False
    assert cloud_backup.backup_enabled(S_no_tg()) is False


# --------------------------------------------------------------------------
# send path
# --------------------------------------------------------------------------

async def test_send_db_backup_posts_document(tmp_path):
    db = tmp_path / "arb_bot.db"
    _make_ledger(db)
    client = FakeClient(
        [("sendDocument", FakeResponse({"result": {"document": {"file_id": "F123"}}}))]
    )
    file_id = await cloud_backup.send_db_backup(db, "TOK", "CHAT", client=client)
    assert file_id == "F123"
    posts = [c for c in client.calls if c[0] == "post"]
    assert len(posts) == 1
    files = posts[0][2]["files"]
    assert files["document"][0] == cloud_backup.BACKUP_FILENAME
    assert posts[0][2]["data"]["chat_id"] == "CHAT"


async def test_send_db_backup_noop_without_telegram(tmp_path):
    db = tmp_path / "arb_bot.db"
    _make_ledger(db)
    client = FakeClient()
    assert await cloud_backup.send_db_backup(db, None, "CHAT", client=client) is None
    assert await cloud_backup.send_db_backup(db, "TOK", None, client=client) is None
    assert await cloud_backup.send_db_backup(tmp_path / "missing.db", "TOK", "CHAT", client=client) is None
    assert client.calls == []


# --------------------------------------------------------------------------
# restore path
# --------------------------------------------------------------------------

async def test_restore_noop_when_ledger_exists(tmp_path):
    db = tmp_path / "arb_bot.db"
    _make_ledger(db)
    client = FakeClient()  # would fail loudly if any call happened
    assert await cloud_backup.restore_if_needed(db, "TOK", "CHAT", timeout_s=1, client=client) == "noop"
    assert client.calls == []


async def test_restore_from_forwarded_document(tmp_path):
    db = tmp_path / "arb_bot.db"
    payload = _make_db_bytes()
    client = FakeClient(
        [
            ("sendMessage", FakeResponse({})),
            ("getUpdates", FakeResponse({"result": [_doc_update("FILE_1")]})),
            ("getFile", FakeResponse({"result": {"file_path": "docs/arb_bot_backup.db"}})),
            ("/file/", FakeResponse(content=payload)),
        ]
    )
    outcome = await cloud_backup.restore_if_needed(db, "TOK", "CHAT", timeout_s=5, client=client)
    assert outcome == "restored"
    assert db.exists()
    assert cloud_backup._validate_sqlite(db)
    con = sqlite3.connect(str(db))
    assert con.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    con.close()


async def test_restore_fresh_reply(tmp_path):
    db = tmp_path / "arb_bot.db"
    client = FakeClient(
        [
            ("sendMessage", FakeResponse({})),
            ("getUpdates", FakeResponse({"result": [{"update_id": 1, "message": {"text": "fresh"}}]})),
        ]
    )
    assert await cloud_backup.restore_if_needed(db, "TOK", "CHAT", timeout_s=5, client=client) == "fresh"
    assert not db.exists()
    # The user must get a confirmation that "fresh" was received — previously
    # this returned silently and the user thought it didn't work.
    confirms = [c for c in client.calls if c[0] == "post" and "sendMessage" in c[1]]
    assert len(confirms) == 2  # the restore prompt + the confirmation
    assert "fresh" in confirms[1][2]["data"]["text"].lower()


async def test_restore_timeout(tmp_path):
    db = tmp_path / "arb_bot.db"
    client = FakeClient([("sendMessage", FakeResponse({}))])  # getUpdates -> empty {}
    outcome = await cloud_backup.restore_if_needed(db, "TOK", "CHAT", timeout_s=0.3, client=client)
    assert outcome == "timeout"
    assert not db.exists()
    # The user must be told the window closed and a fresh account was started.
    confirms = [c for c in client.calls if c[0] == "post" and "sendMessage" in c[1]]
    assert len(confirms) == 2  # the restore prompt + the timeout notice


async def test_restore_without_telegram_starts_fresh(tmp_path):
    db = tmp_path / "arb_bot.db"
    client = FakeClient()
    assert await cloud_backup.restore_if_needed(db, None, None, timeout_s=1, client=client) == "fresh"
    assert client.calls == []
