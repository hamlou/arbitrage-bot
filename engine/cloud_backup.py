"""
Cloud persistence for the paper-validation run on ephemeral hosts (Render free).

Render's free tier wipes the local filesystem on every restart / redeploy /
spin-down (verified against docs.render.com/docs/free), so a local SQLite
ledger would silently vanish mid-run. This module gives the bot a free,
no-credit-card persistence layer using the Telegram Bot API the bot already
uses for alerts:

  * Every CLOUD_BACKUP_INTERVAL_MIN minutes, a *consistent* snapshot of
    storage/arb_bot.db (made with SQLite's online-backup API, safe while the
    bot is mid-write) is sent to TELEGRAM_CHAT_ID as a document named
    ``arb_bot_backup.db``. The user always holds a copy they can see.

  * On boot with a missing/empty DB (fresh Render instance after a disk wipe),
    the bot posts a short message asking the user to forward the latest
    backup document back to it (or reply "fresh" to start clean), polls
    getUpdates for it, downloads it, validates it, and restores it BEFORE the
    trading loop opens the ledger.

Everything in this module is inert unless CLOUD_BACKUP_ENABLED=true AND
Telegram credentials are configured, so local runs are completely unaffected.

Design notes
------------
* Restore relies on the user *forwarding* the backup back to the bot: a bot
  never receives updates about messages it sent itself, and getUpdates cannot
  re-fetch already-consumed updates, so there is no way for the same bot to
  read its own sent documents. Forwarding is a 5-second manual step and
  happens only after a Render restart with a wiped disk (rare).
* Snapshots use the sqlite3 online-backup API, so a copy taken while the bot
  is writing is still internally consistent (no torn pages).
* Network calls go through plain httpx (already a project dependency) rather
  than python-telegram-bot so the module is decoupled and easily testable.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

BACKUP_FILENAME = "arb_bot_backup.db"
API_BASE = "https://api.telegram.org"
FILE_BASE = "https://api.telegram.org/file"

# Defaults, overridable via Settings / environment.
DEFAULT_INTERVAL_MIN = 15.0
DEFAULT_RESTORE_TIMEOUT_S = 120.0


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def backup_enabled(settings: Any) -> bool:
    """True only when the caller explicitly opted in AND Telegram works."""
    return bool(
        getattr(settings, "CLOUD_BACKUP_ENABLED", False)
        and getattr(settings, "TELEGRAM_BOT_TOKEN", None)
        and getattr(settings, "TELEGRAM_CHAT_ID", None)
    )


def db_missing_or_empty(db_path: Path) -> bool:
    """True if the ledger doesn't exist yet or has no tables at all."""
    if not Path(db_path).exists():
        return True
    try:
        con = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            row = con.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()
        finally:
            con.close()
        return row is None or row[0] == 0
    except sqlite3.Error:
        return True


def make_snapshot(db_path: Path, dest_path: Path) -> None:
    """Consistent copy of a live SQLite ledger via the online-backup API."""
    src = sqlite3.connect(str(db_path), timeout=10.0)
    dst = sqlite3.connect(str(dest_path), timeout=10.0)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()


def _ledger_signature(db_path: Path) -> str:
    """Cheap change-detector over the DB file (+ WAL sidecar if present)."""
    h = hashlib.sha256()
    for p in (Path(db_path), Path(str(db_path) + "-wal")):
        try:
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 16), b""):
                    h.update(chunk)
        except OSError:
            pass
    return h.hexdigest()


def _validate_sqlite(path: Path) -> bool:
    """True if the file is a readable, internally consistent SQLite DB."""
    try:
        con = sqlite3.connect(str(path), timeout=5.0)
        try:
            return con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            con.close()
    except sqlite3.Error:
        return False


# --------------------------------------------------------------------------
# send path
# --------------------------------------------------------------------------

async def send_db_backup(
    db_path: Path,
    bot_token: Optional[str],
    chat_id: Optional[str],
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[str]:
    """Send a consistent snapshot of the ledger to the chat.

    Returns the Telegram ``file_id`` on success, None if disabled or the DB
    doesn't exist yet.
    """
    if not (bot_token and chat_id):
        return None
    db_path = Path(db_path)
    if not db_path.exists():
        return None

    tmp = db_path.with_name(BACKUP_FILENAME + ".snapshot.tmp")
    try:
        make_snapshot(db_path, tmp)
        own = client is None
        if own:
            client = httpx.AsyncClient(timeout=60.0)
        try:
            with open(tmp, "rb") as fh:
                files = {"document": (BACKUP_FILENAME, fh, "application/octet-stream")}
                data = {
                    "chat_id": chat_id,
                    "caption": f"auto-backup {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
                }
                r = await client.post(
                    f"{API_BASE}/bot{bot_token}/sendDocument",
                    data=data,
                    files=files,
                )
            r.raise_for_status()
            return r.json()["result"]["document"]["file_id"]
        finally:
            if own:
                await client.aclose()
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# restore path
# --------------------------------------------------------------------------

async def _get_updates(
    client: httpx.AsyncClient,
    bot_token: str,
    offset: Optional[int],
) -> list[dict]:
    r = await client.get(
        f"{API_BASE}/bot{bot_token}/getUpdates",
        params={"timeout": 40, "offset": offset},
        timeout=60.0,
    )
    r.raise_for_status()
    return r.json().get("result", []) or []


async def _download_file(
    client: httpx.AsyncClient, bot_token: str, file_id: str
) -> bytes:
    r = await client.get(
        f"{API_BASE}/bot{bot_token}/getFile", params={"file_id": file_id}, timeout=60.0
    )
    r.raise_for_status()
    file_path = r.json()["result"]["file_path"]
    dl = await client.get(f"{FILE_BASE}/bot{bot_token}/{file_path}", timeout=120.0)
    dl.raise_for_status()
    return dl.content


async def restore_if_needed(
    db_path: Path,
    bot_token: Optional[str],
    chat_id: Optional[str],
    timeout_s: float = DEFAULT_RESTORE_TIMEOUT_S,
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    """Restore the ledger from a forwarded backup if the local one is missing.

    Returns one of:
      "noop"    — a usable ledger already exists; nothing done
      "fresh"   — nothing to restore (first deploy / user chose fresh)
      "restored"— downloaded, validated, and written a forwarded backup
      "corrupt" — a backup arrived but failed validation; deleted
      "timeout" — no usable backup arrived within ``timeout_s``
    """
    db_path = Path(db_path)
    if not db_missing_or_empty(db_path):
        return "noop"
    if not (bot_token and chat_id):
        return "fresh"

    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=60.0)
    try:
        # Tell the user what to do. Forwarding is required because a bot can
        # never read its own sent messages.
        r = await client.post(
            f"{API_BASE}/bot{bot_token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": (
                    "🔄 Cloud restart detected — this host's disk was wiped "
                    "(normal on free cloud tiers), so the bot's database is "
                    "gone.\n\n"
                    "If this is a fresh deploy, reply: fresh\n"
                    "Otherwise, forward the most recent "
                    f"<b>{BACKUP_FILENAME}</b> document I sent you and I'll "
                    "restore it automatically."
                ),
                "parse_mode": "HTML",
            },
            timeout=60.0,
        )
        r.raise_for_status()

        offset: Optional[int] = None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            updates = await _get_updates(client, bot_token, offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message") or {}
                doc = msg.get("document") or {}
                if (doc.get("file_name") or "").endswith(BACKUP_FILENAME):
                    try:
                        payload = await _download_file(client, bot_token, doc["file_id"])
                    except Exception:
                        logger.exception("Backup download failed; continuing to wait")
                        continue
                    db_path.parent.mkdir(parents=True, exist_ok=True)
                    db_path.write_bytes(payload)
                    # Drop any stale WAL/shm sidecars left by a dead instance.
                    for side in (Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
                        side.unlink(missing_ok=True)
                    if _validate_sqlite(db_path):
                        logger.info("Restored ledger from forwarded Telegram backup (%d bytes)", len(payload))
                        return "restored"
                    logger.warning("Forwarded backup failed validation; deleting")
                    db_path.unlink(missing_ok=True)
                    return "corrupt"
                text = (msg.get("text") or "").strip().lower()
                if text in {"fresh", "/fresh", "start fresh", "fresh start"}:
                    return "fresh"
            await asyncio.sleep(1.0)
        return "timeout"
    finally:
        if own:
            await client.aclose()
