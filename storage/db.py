"""
Async SQLite storage layer, built on aiosqlite. One connection is opened per
call by default (fine at this trade volume); a persistent-connection mode is
available via Database.connect() for the long-running main loop.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._enable_wal(self._conn)
        await self._init_schema(self._conn)

    async def _enable_wal(self, conn: aiosqlite.Connection) -> None:
        """
        Switch the database to WAL journal mode (idempotent — persists in the
        DB file, so a connect() to an existing WAL database is a no-op). WAL
        lets the trading loop's readers keep reading while the writer commits,
        instead of blocking on a shared lock for the duration of each write.
        """
        cur = await conn.execute("PRAGMA journal_mode=WAL")
        row = await cur.fetchone()
        mode = row[0] if row else "?"
        if mode != "wal":
            # e.g. some network filesystems silently fall back to "delete" or
            # "memory" instead of raising — make that visible rather than
            # silently proceeding without the WAL behavior we asked for.
            logger.warning(
                "SQLite journal_mode for %s is %r, not WAL — concurrent readers "
                "may block on the writer lock",
                self.db_path, mode,
            )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def connected(self) -> bool:
        """Whether a persistent connection is open. Audit-path callers (e.g.
        the signal engine's cross-exchange disagreement writer) use this to
        skip writes when the DB isn't connected — scripts like backtest.py
        run evaluate() against a deliberately unconnected Database(:memory:)
        and must never raise or pollute logs from an observational write."""
        return self._conn is not None

    async def _init_schema(self, conn: aiosqlite.Connection) -> None:
        # Column migrations must run BEFORE the schema script: schema.sql
        # includes `CREATE INDEX ... ON trades(combo_group_id)`, which fails
        # immediately if `trades` already exists (from an older schema
        # version) without that column yet.
        await self._migrate_missing_columns(conn)
        schema_sql = SCHEMA_PATH.read_text()
        await conn.executescript(schema_sql)
        await conn.commit()

    async def _migrate_missing_columns(self, conn: aiosqlite.Connection) -> None:
        """
        CREATE TABLE IF NOT EXISTS only creates tables that don't exist yet —
        it does NOT retroactively add new columns to a table from an older
        schema version. If you ran this project before the strategy/
        combo_group_id columns were added to `trades`, this adds them so
        existing DB files don't break. Safe to run every startup: checks
        whether `trades` exists at all first (a brand-new DB has nothing to
        migrate — the schema script below creates it correctly from scratch),
        then checks pragma table_info before adding anything.
        """
        cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trades'")
        if await cur.fetchone() is None:
            return  # fresh DB — schema script will create it with all columns already

        cur = await conn.execute("PRAGMA table_info(trades)")
        existing_columns = {row[1] for row in await cur.fetchall()}

        migrations = {
            "strategy": "ALTER TABLE trades ADD COLUMN strategy TEXT NOT NULL DEFAULT 'latency_arb'",
            "combo_group_id": "ALTER TABLE trades ADD COLUMN combo_group_id TEXT",
            # Fill-realism measurement (2026-08-07): the broker computes
            # slippage and decision/fill best-asks on every fill but never
            # persisted them — the single most useful paper-mode metric
            # ("how much edge did we lose between deciding and filling?") was
            # computed and thrown away. Add the columns so existing DBs get
            # them too.
            "slippage_pct": "ALTER TABLE trades ADD COLUMN slippage_pct REAL",
            "decision_best_ask": "ALTER TABLE trades ADD COLUMN decision_best_ask REAL",
            "fill_best_ask": "ALTER TABLE trades ADD COLUMN fill_best_ask REAL",
            # Exit-excursion measurement (2026-08-11): max favorable/adverse
            # excursion per trade, tracked in main.py and persisted at close.
            "mfe_pct": "ALTER TABLE trades ADD COLUMN mfe_pct REAL",
            "mae_pct": "ALTER TABLE trades ADD COLUMN mae_pct REAL",
        }
        for column, ddl in migrations.items():
            if column not in existing_columns:
                logger.info("Migrating trades table: adding missing column '%s'", column)
                await conn.execute(ddl)
        await conn.commit()

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected — call `await db.connect()` first")
        return self._conn

    # -- signals -------------------------------------------------------------

    async def log_signal(
        self,
        *,
        market_id: str,
        asset: str,
        implied_prob: float,
        polymarket_prob: float,
        edge_pct: float,
        confidence: float,
        fired: bool,
        reason: str = "",
        binance_tick_age_s: Optional[float] = None,
        book_depth_usd: Optional[float] = None,
    ) -> int:
        conn = self._require_conn()
        cur = await conn.execute(
            """
            INSERT INTO signals
                (ts, market_id, asset, implied_prob, polymarket_prob, edge_pct,
                 confidence, fired, reason, binance_tick_age_s, book_depth_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                time.time(), market_id, asset, implied_prob, polymarket_prob,
                edge_pct, confidence, int(fired), reason, binance_tick_age_s,
                book_depth_usd,
            ),
        )
        await conn.commit()
        return cur.lastrowid

    async def get_signals(self, *, market_id: Optional[str] = None, since_ts: float = 0.0) -> list[dict[str, Any]]:
        """
        Read path for logged signals. Paper trades store signal_id=None (see
        broker_paper.place_order), so audit scripts that need the model's
        predicted probability at the time a trade was opened match by
        market_id + entry_ts instead of by foreign key — this returns every
        signal row (optionally for one market, since a timestamp), oldest
        first, so the caller can pick the last one before a trade's entry.
        """
        conn = self._require_conn()
        if market_id:
            cur = await conn.execute(
                "SELECT * FROM signals WHERE ts >= ? AND market_id = ? ORDER BY ts",
                (since_ts, market_id),
            )
        else:
            cur = await conn.execute("SELECT * FROM signals WHERE ts >= ? ORDER BY ts", (since_ts,))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # -- trades ----------------------------------------------------------

    async def open_trade(
        self,
        *,
        signal_id: Optional[int],
        market_id: str,
        asset: str,
        side: str,
        mode: str,
        entry_price: float,
        size_usd: float,
        fee_usd: float,
        strategy: str = "latency_arb",
        combo_group_id: Optional[str] = None,
        slippage_pct: Optional[float] = None,
        decision_best_ask: Optional[float] = None,
        fill_best_ask: Optional[float] = None,
    ) -> int:
        conn = self._require_conn()
        cur = await conn.execute(
            """
            INSERT INTO trades
                (signal_id, market_id, asset, side, mode, strategy, combo_group_id,
                 entry_ts, entry_price, size_usd, fee_usd,
                 slippage_pct, decision_best_ask, fill_best_ask, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
            """,
            (signal_id, market_id, asset, side, mode, strategy, combo_group_id,
             time.time(), entry_price, size_usd, fee_usd,
             slippage_pct, decision_best_ask, fill_best_ask),
        )
        await conn.commit()
        return cur.lastrowid

    async def close_trade(
        self,
        trade_id: int,
        *,
        exit_price: float,
        exit_reason: str,
        realized_pnl_usd: float,
    ) -> None:
        conn = self._require_conn()
        await conn.execute(
            """
            UPDATE trades
            SET exit_ts = ?, exit_price = ?, exit_reason = ?,
                realized_pnl_usd = ?, status = 'CLOSED'
            WHERE id = ?
            """,
            (time.time(), exit_price, exit_reason, realized_pnl_usd, trade_id),
        )
        await conn.commit()

    async def get_trade(self, trade_id: int) -> Optional[dict[str, Any]]:
        conn = self._require_conn()
        cur = await conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def set_trade_excursion(self, trade_id: int, mfe_pct: float, mae_pct: float) -> None:
        """Persist the tracked max favorable / adverse excursion at trade close."""
        conn = self._require_conn()
        await conn.execute(
            "UPDATE trades SET mfe_pct = ?, mae_pct = ? WHERE id = ?",
            (mfe_pct, mae_pct, trade_id),
        )
        await conn.commit()

    async def insert_exit_probe(
        self,
        trade_id: int,
        sample_label: str,
        quote_price: float,
        entry_price: float,
        outcome: Optional[str] = None,
    ) -> None:
        conn = self._require_conn()
        await conn.execute(
            "INSERT INTO exit_probes (trade_id, ts, sample_label, quote_price, entry_price, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (trade_id, time.time(), sample_label, quote_price, entry_price, outcome),
        )
        await conn.commit()

    async def get_exit_probes(self, trade_id: Optional[int] = None) -> list[dict[str, Any]]:
        conn = self._require_conn()
        if trade_id is not None:
            cur = await conn.execute(
                "SELECT * FROM exit_probes WHERE trade_id = ? ORDER BY ts", (trade_id,)
            )
        else:
            cur = await conn.execute("SELECT * FROM exit_probes ORDER BY trade_id, ts")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_open_trades(self, mode: Optional[str] = None) -> list[dict[str, Any]]:
        conn = self._require_conn()
        if mode:
            cur = await conn.execute(
                "SELECT * FROM trades WHERE status = 'OPEN' AND mode = ?", (mode,)
            )
        else:
            cur = await conn.execute("SELECT * FROM trades WHERE status = 'OPEN'")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_all_trades(self, mode: Optional[str] = None) -> list[dict[str, Any]]:
        conn = self._require_conn()
        if mode:
            cur = await conn.execute("SELECT * FROM trades WHERE mode = ? ORDER BY entry_ts", (mode,))
        else:
            cur = await conn.execute("SELECT * FROM trades ORDER BY entry_ts")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # -- equity curve --------------------------------------------------------

    async def record_equity(self, *, mode: str, balance_usd: float, unrealized_pnl_usd: float = 0.0) -> None:
        conn = self._require_conn()
        await conn.execute(
            "INSERT INTO equity_curve (ts, mode, balance_usd, unrealized_pnl_usd) VALUES (?, ?, ?, ?)",
            (time.time(), mode, balance_usd, unrealized_pnl_usd),
        )
        await conn.commit()

    async def get_equity_curve(self, mode: Optional[str] = None) -> list[dict[str, Any]]:
        conn = self._require_conn()
        if mode:
            cur = await conn.execute("SELECT * FROM equity_curve WHERE mode = ? ORDER BY ts", (mode,))
        else:
            cur = await conn.execute("SELECT * FROM equity_curve ORDER BY ts")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # -- risk events -----------------------------------------------------

    async def log_risk_event(
        self,
        *,
        event_type: str,
        detail: str = "",
        balance_usd: Optional[float] = None,
        drawdown_pct: Optional[float] = None,
    ) -> None:
        conn = self._require_conn()
        await conn.execute(
            """
            INSERT INTO risk_events (ts, event_type, detail, balance_usd, drawdown_pct)
            VALUES (?, ?, ?, ?, ?)
            """,
            (time.time(), event_type, detail, balance_usd, drawdown_pct),
        )
        await conn.commit()

    async def get_risk_events(self) -> list[dict[str, Any]]:
        conn = self._require_conn()
        cur = await conn.execute("SELECT * FROM risk_events ORDER BY ts DESC")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_latest_risk_flag(self, event_type: str) -> Optional[dict[str, Any]]:
        """Most recent row of a given event_type — used to check halt/kill state on startup."""
        conn = self._require_conn()
        cur = await conn.execute(
            "SELECT * FROM risk_events WHERE event_type = ? ORDER BY ts DESC LIMIT 1",
            (event_type,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    # -- latency events --------------------------------------------------

    async def log_latency_event(
        self,
        *,
        market_id: str,
        tick_received_at: float,
        signal_evaluated_at: float,
        order_submitted_at: Optional[float],
        fired: bool,
    ) -> None:
        conn = self._require_conn()
        tick_to_signal_ms = (signal_evaluated_at - tick_received_at) * 1000
        signal_to_order_ms = (
            (order_submitted_at - signal_evaluated_at) * 1000 if order_submitted_at else None
        )
        tick_to_order_ms = (
            (order_submitted_at - tick_received_at) * 1000 if order_submitted_at else None
        )
        await conn.execute(
            """
            INSERT INTO latency_events
                (market_id, tick_received_at, signal_evaluated_at, order_submitted_at,
                 tick_to_signal_ms, signal_to_order_ms, tick_to_order_ms, fired)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market_id, tick_received_at, signal_evaluated_at, order_submitted_at,
                tick_to_signal_ms, signal_to_order_ms, tick_to_order_ms, int(fired),
            ),
        )
        await conn.commit()

    async def get_latency_events(self, fired_only: bool = False) -> list[dict[str, Any]]:
        conn = self._require_conn()
        if fired_only:
            cur = await conn.execute("SELECT * FROM latency_events WHERE fired = 1 ORDER BY id")
        else:
            cur = await conn.execute("SELECT * FROM latency_events ORDER BY id")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # -- exchange disagreements -------------------------------------------

    async def log_exchange_disagreement(
        self,
        *,
        symbol: str,
        binance_price: float,
        coinbase_price: float,
        disagreement_pct: float,
    ) -> int:
        """
        Record one observed cross-exchange price disagreement (called from the
        signal engine's gate whenever the Binance/Coinbase difference exceeds
        CROSS_EXCHANGE_TOLERANCE_PCT — regardless of whether it blocked a
        signal), so disagreement frequency can be reviewed later.
        """
        conn = self._require_conn()
        cur = await conn.execute(
            """
            INSERT INTO exchange_disagreements
                (ts, symbol, binance_price, coinbase_price, disagreement_pct)
            VALUES (?, ?, ?, ?, ?)
            """,
            (time.time(), symbol, binance_price, coinbase_price, disagreement_pct),
        )
        await conn.commit()
        return cur.lastrowid

    async def get_exchange_disagreements(
        self, *, symbol: Optional[str] = None, limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Most recent exchange_disagreements rows, newest first. Optionally
        filtered to one symbol."""
        conn = self._require_conn()
        if symbol:
            cur = await conn.execute(
                "SELECT * FROM exchange_disagreements WHERE symbol = ? ORDER BY ts DESC LIMIT ?",
                (symbol, limit),
            )
        else:
            cur = await conn.execute(
                "SELECT * FROM exchange_disagreements ORDER BY ts DESC LIMIT ?", (limit,)
            )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # -- lag events (empirical arbitrage-window measurement) -----------------

    async def log_lag_event(
        self,
        *,
        asset: str,
        move_pct: float,
        move_dir: str,
        token_id: str,
        binance_move_ts: float,
        baseline_mid: Optional[float] = None,
        poly_repriced_ts: Optional[float] = None,
        poly_move_pct: Optional[float] = None,
        timed_out: int = 0,
        lag_ms: Optional[float] = None,
    ) -> None:
        """Record one measured Binance-move -> Polymarket-reprice lag. Written
        by main.py's lag tracker loop; pure diagnostics, never read by any
        trading decision."""
        conn = self._require_conn()
        await conn.execute(
            """
            INSERT INTO lag_events
                (ts, asset, move_pct, move_dir, token_id, binance_move_ts,
                 baseline_mid, poly_repriced_ts, poly_move_pct, timed_out, lag_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (time.time(), asset, move_pct, move_dir, token_id, binance_move_ts,
             baseline_mid, poly_repriced_ts, poly_move_pct, timed_out, lag_ms),
        )
        await conn.commit()

    async def get_lag_events(self, since_ts: float = 0.0) -> list[dict[str, Any]]:
        """All lag measurements (or since a timestamp), oldest first."""
        conn = self._require_conn()
        cur = await conn.execute(
            "SELECT * FROM lag_events WHERE ts >= ? ORDER BY ts", (since_ts,)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
