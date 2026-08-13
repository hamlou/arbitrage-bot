"""
Command Center API — the brain of the arb bot, exposed.

Read-only FastAPI server. It never writes to the bot's database and never
touches the trading loop. Everything it serves comes from two sources:

  1. The bot's SQLite ledger (storage/arb_bot.db) — the persistent truth:
     trades, signals, equity curve, latency events, risk events,
     exchange disagreements.
  2. The bot's live state file (command_center/api/live_state.json) — written
     by main.py every ~2 seconds while the bot runs. When the bot is offline
     the file goes stale and the API degrades gracefully (bot_status: offline)
     while still serving full historical data.

Run with:  .venv/Scripts/python.exe -m uvicorn command_center.api.main:app --port 8787
(executed from the polymarket-arb-bot directory, or via command_center/start_api.bat)
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "storage" / "arb_bot.db"
STATE_PATH = ROOT / "command_center" / "api" / "live_state.json"

app = FastAPI(title="Arbitrage Bot Command Center", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # dev server; tighten before deploying
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    """New read-only-ish connection per request. The bot uses WAL journaling,
    so a long-lived reader here never blocks the writer. Only SELECTs ever
    run through this connection."""
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _q(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Run a read query; errors are logged loudly (never silently empty)."""
    conn = _db()
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        logger.exception("Command Center SQL query failed: %s", sql)
        return []
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _q1(sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
    rows = _q(sql, params)
    return rows[0] if rows else None


def _percentile(vals: list[float], p: float) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * p
    f, c = int(math.floor(k)), min(int(math.ceil(k)), len(s) - 1)
    return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)


def _now() -> float:
    return time.time()


def _to_float(v: Any) -> Optional[float]:
    """Best-effort numeric coercion. The bot's state file carries config
    values as formatted strings ("250ms", "2.0s"), so a raw float() would
    crash — pull the leading number instead."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(\.\d+)?", str(v))
    return float(m.group()) if m else None


def _read_state() -> dict[str, Any]:
    """Read the bot's live state file. Returns {} when missing/corrupt."""
    try:
        if not STATE_PATH.exists():
            return {}
        raw = STATE_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def _state_age_s() -> Optional[float]:
    try:
        if not STATE_PATH.exists():
            return None
        return max(0.0, _now() - STATE_PATH.stat().st_mtime)
    except Exception:
        return None


class Snapshot(BaseModel):
    """The payload pushed to WebSocket clients."""
    type: str = "snapshot"
    ts: float = 0.0
    overview: dict[str, Any] = {}
    activity: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    live: dict[str, Any] = {}


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict[str, Any]:
    age = _state_age_s()
    return {
        "ok": True,
        "server_time": _now(),
        "db": "ok" if DB_PATH.exists() else "missing",
        "state_file_age_s": age,
        "bot_status": "online" if (age is not None and age < 15.0) else "offline",
        "version": "1.0.0",
    }


@app.get("/api/overview")
def overview() -> dict[str, Any]:
    """One call that powers the entire home screen."""
    state = _read_state()
    age = _state_age_s()
    bot_online = age is not None and age < 15.0

    # -- account -------------------------------------------------------------
    trades = _q("SELECT * FROM trades ORDER BY id")
    closed = [t for t in trades if t.get("status") == "CLOSED"]
    open_trades = [t for t in trades if t.get("status") == "OPEN"]
    pnls = [t.get("realized_pnl_usd") or 0.0 for t in closed]
    total_pnl = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    win_rate = (wins / len(pnls) * 100.0) if pnls else None
    avg_win = (sum(p for p in pnls if p > 0) / wins) if wins else None
    avg_loss = (sum(p for p in pnls if p < 0) / losses) if losses else None
    profit_factor = (
        sum(p for p in pnls if p > 0) / abs(sum(p for p in pnls if p < 0))
        if sum(p for p in pnls if p < 0) != 0 else None
    )

    # equity: the TRADE LEDGER is the truth (same philosophy as
    # broker_paper.load_open_positions — the equity curve was corrupted by an
    # old restart bug, so it is never the primary source). Ledger math only
    # applies to PAPER mode (LIVE trades have no DB rows); LIVE falls back to
    # the equity curve's last point.
    from config.settings import settings as bot_settings

    mode = state.get("mode") or "PAPER"
    equity_rows = _q("SELECT * FROM equity_curve ORDER BY id DESC LIMIT 1")
    ledger_equity = None
    if mode == "PAPER":
        ledger_equity = bot_settings.STARTING_PAPER_BALANCE_USD + total_pnl - sum(
            (t.get("size_usd") or 0.0) + (t.get("fee_usd") or 0.0) for t in open_trades
        )
    equity_usd = ledger_equity
    if equity_usd is None:
        equity_usd = equity_rows[0].get("balance_usd") if equity_rows else None

    state_account = state.get("account") or {}
    live_balance = state_account.get("balance_usd")
    live_equity = state_account.get("equity_usd")
    feed_detail = state.get("feed_detail") or {}
    risk_detail = state.get("risk_detail") or {}

    account = {
        "mode": mode,
        "balance_usd": live_balance if bot_online else equity_usd,
        "equity_usd": live_equity if bot_online else equity_usd,
        "total_pnl_usd": total_pnl,
        "win_rate_pct": win_rate,
        "closed_trades": len(closed),
        "open_positions": len(open_trades),
        "wins": wins,
        "losses": losses,
        "avg_win_usd": avg_win,
        "avg_loss_usd": avg_loss,
        "profit_factor": profit_factor,
        "uptime_s": state.get("uptime_s"),
        "paused": state.get("paused", False),
        "alerts_muted": state.get("alerts_muted", False),
        "daily_halted": state.get("daily_halted", False),
        "kill_switch_tripped": state.get("kill_switch_tripped", False),
        "daily_pnl_pct": risk_detail.get("daily_pnl_pct"),
        "drawdown_pct": risk_detail.get("drawdown_pct"),
    }

    # -- feeds ----------------------------------------------------------------
    # When the bot is offline we report healthy=None ("unknown"), NEVER
    # healthy=False — a stopped bot is not the same as a dead feed, and the
    # UI renders null as a muted "unknown" instead of a scary red "down".
    if bot_online:
        feeds = {
            "binance": {
                "healthy": bool(state.get("binance_feed_healthy", False)),
                "reconnects_10m": (feed_detail.get("binance") or {}).get("reconnects_10m", 0),
                "stale_s": (feed_detail.get("binance") or {}).get("stale_s"),
            },
            "polymarket": {
                "healthy": bool(state.get("polymarket_feed_healthy", False)),
                "reconnects_10m": (feed_detail.get("polymarket") or {}).get("reconnects_10m", 0),
                "stale_s": (feed_detail.get("polymarket") or {}).get("stale_s"),
            },
        }
    else:
        feeds = {
            "binance": {"healthy": None, "reconnects_10m": 0, "stale_s": None},
            "polymarket": {"healthy": None, "reconnects_10m": 0, "stale_s": None},
        }

    # -- latency ---------------------------------------------------------------
    latency_events = _q("SELECT * FROM latency_events")
    t2s = [e["tick_to_signal_ms"] for e in latency_events if e.get("tick_to_signal_ms") is not None]
    t2o = [e["tick_to_order_ms"] for e in latency_events if e.get("tick_to_order_ms") is not None]
    s2o = [e["signal_to_order_ms"] for e in latency_events if e.get("signal_to_order_ms") is not None]
    latency = {
        "tick_to_signal_p50_ms": _percentile(t2s, 0.50),
        "tick_to_signal_p95_ms": _percentile(t2s, 0.95),
        "tick_to_order_p50_ms": _percentile(t2o, 0.50),
        "tick_to_order_p95_ms": _percentile(t2o, 0.95),
        "signal_to_order_p95_ms": _percentile(s2o, 0.95),
        "cycles": len(latency_events),
        "fired": sum(1 for e in latency_events if e.get("fired")),
    }
    cfg = state.get("config") or {}
    latency.update(
        {
            "platform_delay_ms": _to_float(cfg.get("platform_taker_delay_ms")),
            "window_s": _to_float(cfg.get("arb_window_s")),
        }
    )
    verdict = "n/a"
    p95_order = latency.get("tick_to_order_p95_ms")
    if p95_order is not None:
        platform = float(latency.get("platform_delay_ms") or 0)
        window = float(latency.get("window_s") or 2.0) * 1000
        total = p95_order + platform
        verdict = "comfortable" if total < window * 0.5 else ("tight" if total < window else "too slow")
    latency["verdict"] = verdict

    # -- strategy breakdown ----------------------------------------------------
    by_strategy: dict[str, dict[str, Any]] = {}
    for t in closed:
        strat = t.get("strategy") or "latency_arb"
        b = by_strategy.setdefault(strat, {"strategy": strat, "trades": 0, "pnl_usd": 0.0, "wins": 0, "losses": 0})
        b["trades"] += 1
        pnl = t.get("realized_pnl_usd") or 0.0
        b["pnl_usd"] += pnl
        if pnl > 0:
            b["wins"] += 1
        elif pnl < 0:
            b["losses"] += 1
    for b in by_strategy.values():
        b["win_rate_pct"] = b["wins"] / b["trades"] * 100.0 if b["trades"] else None
        b["avg_pnl_usd"] = b["pnl_usd"] / b["trades"] if b["trades"] else None

    # -- signals today ---------------------------------------------------------
    today_start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    signals = _q("SELECT * FROM signals WHERE ts >= ? ORDER BY ts DESC LIMIT 500", (today_start,))
    signals_fired = [s for s in signals if s.get("fired")]
    sig_total = _q1("SELECT COUNT(*) AS n FROM signals WHERE ts >= ?", (today_start,))

    # -- recent trades ---------------------------------------------------------
    recent = sorted(
        closed, key=lambda t: t.get("exit_ts") or t.get("entry_ts") or 0, reverse=True,
    )[:10]

    markets = state.get("markets") or []
    positions_state = state.get("positions") or []

    return {
        "ts": _now(),
        "bot_online": bot_online,
        "state_age_s": age,
        "account": account,
        "feeds": feeds,
        "risk": {
            "daily_halted": account["daily_halted"],
            "kill_switch_tripped": account["kill_switch_tripped"],
            "daily_pnl_pct": account["daily_pnl_pct"],
            "drawdown_pct": account["drawdown_pct"],
            "daily_halt_threshold_pct": (risk_detail.get("daily_halt_threshold_pct")),
            "kill_threshold_pct": risk_detail.get("kill_threshold_pct"),
        },
        "latency": latency,
        "strategy": list(by_strategy.values()),
        "signals_today": {"total": (sig_total or {}).get("n", 0), "fired": len(signals_fired)},
        "sum_to_one_scan": state.get("sum_to_one_scan"),
        "recent_trades": recent,
        "markets": markets,
        "positions": positions_state,
    }


@app.get("/api/account")
def account() -> dict[str, Any]:
    ov = overview()
    return {"account": ov["account"], "risk": ov["risk"], "ts": ov["ts"]}


@app.get("/api/trades")
def trades(
    status: Optional[str] = Query(None),
    strategy: Optional[str] = Query(None),
    side: Optional[str] = Query(None),
    mode: Optional[str] = Query(None),
    asset: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []
    if status:
        where.append("status = ?")
        params.append(status.upper())
    if strategy:
        where.append("strategy = ?")
        params.append(strategy)
    if side:
        where.append("side = ?")
        params.append(side.upper())
    if mode:
        where.append("mode = ?")
        params.append(mode.upper())
    if asset:
        where.append("asset = ?")
        params.append(asset.upper())
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = _q1(f"SELECT COUNT(*) AS n FROM trades{clause}", tuple(params))
    rows = _q(
        f"SELECT * FROM trades{clause} ORDER BY COALESCE(exit_ts, entry_ts) DESC LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset),
    )
    closed = [r for r in rows if r.get("status") == "CLOSED"]
    pnls = [r.get("realized_pnl_usd") or 0.0 for r in closed]
    stats = {
        "count": len(rows),
        "total": (total or {}).get("n", 0),
        "closed_pnl_usd": sum(pnls),
        "wins": sum(1 for p in pnls if p > 0),
        "losses": sum(1 for p in pnls if p < 0),
    }
    return {"trades": rows, "stats": stats, "limit": limit, "offset": offset}


@app.get("/api/positions")
def positions() -> dict[str, Any]:
    """Open positions. Enriched with live mark prices from the bot's state file
    when it's online (falls back to entry price)."""
    state = _read_state()
    live_positions = {p.get("market_id"): p for p in (state.get("positions") or [])}
    rows = _q("SELECT * FROM trades WHERE status = 'OPEN' ORDER BY entry_ts DESC")
    out = []
    for r in rows:
        mid = r.get("entry_price")
        if r["market_id"] in live_positions:
            mid = live_positions[r["market_id"]].get("mark_price") or mid
        entry = r.get("entry_price") or 0.0
        shares = (r.get("size_usd") or 0.0) / entry if entry else 0.0
        unrealized = (mid - entry) * shares if mid and entry else 0.0
        out.append({**r, "mark_price": mid, "unrealized_pnl_usd": unrealized})
    return {"positions": out, "count": len(out)}


@app.get("/api/equity")
def equity(mode: Optional[str] = None, limit: int = Query(2000, ge=1, le=50000)) -> dict[str, Any]:
    """Profit timeline. For PAPER mode this is reconstructed from the TRADE
    LEDGER (same truth source as broker_paper.load_open_positions) — the raw
    equity_curve table was corrupted by an old restart-reset bug, so plotting
    it would show a phantom +$43 jump. Balance steps:
      open:  balance -= size + fee
      close: balance += realized_pnl + size + fee  (the payout)
    Falls back to the raw curve for LIVE mode / no-trade databases."""
    from config.settings import settings as bot_settings

    trades = _q("SELECT * FROM trades ORDER BY entry_ts")
    if mode:
        trades = [t for t in trades if t.get("mode") == mode.upper()]

    if trades and (mode is None or mode.upper() == "PAPER"):
        events: list[tuple[float, float]] = []
        for t in trades:
            events.append((t["entry_ts"], -((t.get("size_usd") or 0.0) + (t.get("fee_usd") or 0.0))))
            if t.get("status") == "CLOSED" and t.get("exit_ts"):
                events.append((
                    t["exit_ts"],
                    (t.get("realized_pnl_usd") or 0.0) + (t.get("size_usd") or 0.0) + (t.get("fee_usd") or 0.0),
                ))
        events.sort(key=lambda e: e[0])
        points: list[dict[str, Any]] = []
        bal = float(bot_settings.STARTING_PAPER_BALANCE_USD)
        # Start the curve at the initial balance just before the first event,
        # so the timeline reads the full journey ($1,000 → today).
        if events:
            points.append({"ts": events[0][0] - 0.001, "mode": "PAPER", "balance_usd": bal, "unrealized_pnl_usd": 0.0})
        for ts, delta in events:
            bal += delta
            points.append({"ts": ts, "mode": "PAPER", "balance_usd": round(bal, 2), "unrealized_pnl_usd": 0.0})
        return {"points": points[-limit:], "count": len(points)}

    if mode:
        rows = _q("SELECT * FROM equity_curve WHERE mode = ? ORDER BY id DESC LIMIT ?", (mode.upper(), limit))
    else:
        rows = _q("SELECT * FROM equity_curve ORDER BY id DESC LIMIT ?", (limit,))
    rows = list(reversed(rows))
    return {"points": rows, "count": len(rows)}


@app.get("/api/signals")
def signals(
    market_id: Optional[str] = None,
    fired: Optional[bool] = None,
    limit: int = Query(300, ge=1, le=2000),
) -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []
    if market_id:
        where.append("market_id = ?")
        params.append(market_id)
    if fired is not None:
        where.append("fired = ?")
        params.append(1 if fired else 0)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = _q(
        f"SELECT * FROM signals{clause} ORDER BY ts DESC LIMIT ?", tuple(params) + (limit,),
    )
    fired_rows = [r for r in rows if r.get("fired")]
    return {
        "signals": rows,
        "count": len(rows),
        "fired": len(fired_rows),
        "avg_edge_pct": (
            sum(r.get("edge_pct") or 0.0 for r in fired_rows) / len(fired_rows)
            if fired_rows else None
        ),
    }


@app.get("/api/latency")
def latency() -> dict[str, Any]:
    rows = _q("SELECT * FROM latency_events ORDER BY id")
    t2s = [r["tick_to_signal_ms"] for r in rows if r.get("tick_to_signal_ms") is not None]
    t2o = [r["tick_to_order_ms"] for r in rows if r.get("tick_to_order_ms") is not None]
    s2o = [r["signal_to_order_ms"] for r in rows if r.get("signal_to_order_ms") is not None]
    out: dict[str, Any] = {
        "count": len(rows),
        "fired": sum(1 for r in rows if r.get("fired")),
        "tick_to_signal": {
            "p50_ms": _percentile(t2s, 0.50),
            "p75_ms": _percentile(t2s, 0.75),
            "p95_ms": _percentile(t2s, 0.95),
            "p99_ms": _percentile(t2s, 0.99),
            "max_ms": max(t2s) if t2s else None,
        },
        "tick_to_order": {
            "p50_ms": _percentile(t2o, 0.50),
            "p75_ms": _percentile(t2o, 0.75),
            "p95_ms": _percentile(t2o, 0.95),
            "p99_ms": _percentile(t2o, 0.99),
            "max_ms": max(t2o) if t2o else None,
        },
        "signal_to_order": {
            "p50_ms": _percentile(s2o, 0.50),
            "p95_ms": _percentile(s2o, 0.95),
        },
    }
    # recent time series, downsampled to ~120 points
    series = list(reversed(rows[-3000:]))
    step = max(1, len(series) // 120)
    out["series"] = [
        {
            "id": r["id"],
            "tick_to_signal_ms": r.get("tick_to_signal_ms"),
            "tick_to_order_ms": r.get("tick_to_order_ms"),
            "fired": bool(r.get("fired")),
            "market_id": r.get("market_id"),
        }
        for r in series[::step]
    ]
    return out


@app.get("/api/risk-events")
def risk_events(limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
    rows = _q("SELECT * FROM risk_events ORDER BY ts DESC LIMIT ?", (limit,))
    return {"events": rows, "count": len(rows)}


@app.get("/api/disagreements")
def disagreements(symbol: Optional[str] = None, limit: int = Query(200, ge=1, le=2000)) -> dict[str, Any]:
    if symbol:
        rows = _q(
            "SELECT * FROM exchange_disagreements WHERE symbol = ? ORDER BY ts DESC LIMIT ?",
            (symbol, limit),
        )
    else:
        rows = _q("SELECT * FROM exchange_disagreements ORDER BY ts DESC LIMIT ?", (limit,))
    return {"events": rows, "count": len(rows)}


@app.get("/api/activity")
def activity(limit: int = Query(120, ge=1, le=500)) -> dict[str, Any]:
    """Unified timeline: trades (open/close), fired signals, risk events."""
    items: list[dict[str, Any]] = []

    for t in _q("SELECT * FROM trades ORDER BY COALESCE(exit_ts, entry_ts) DESC LIMIT ?", (limit,)):
        ts = t.get("exit_ts") or t.get("entry_ts") or 0
        if t.get("status") == "CLOSED":
            items.append({
                "ts": ts, "type": "trade",
                "kind": "closed", "label": f"{t.get('asset')} {t.get('side')} closed",
                "pnl_usd": t.get("realized_pnl_usd"), "exit_reason": t.get("exit_reason"),
                "market_id": t.get("market_id"), "strategy": t.get("strategy"),
            })
        else:
            items.append({
                "ts": ts, "type": "trade",
                "kind": "opened", "label": f"{t.get('asset')} {t.get('side')} opened",
                "entry_price": t.get("entry_price"), "size_usd": t.get("size_usd"),
                "market_id": t.get("market_id"), "strategy": t.get("strategy"),
            })

    for s in _q("SELECT * FROM signals WHERE fired = 1 ORDER BY ts DESC LIMIT ?", (limit,)):
        items.append({
            "ts": s.get("ts"), "type": "signal",
            "kind": "fired", "label": f"{s.get('asset')} signal fired",
            "edge_pct": s.get("edge_pct"), "confidence": s.get("confidence"),
            "model": s.get("reason"), "market_id": s.get("market_id"),
        })

    for e in _q("SELECT * FROM risk_events ORDER BY ts DESC LIMIT ?", (limit,)):
        items.append({
            "ts": e.get("ts"), "type": "risk",
            "kind": e.get("event_type"), "label": e.get("event_type", "risk event"),
            "detail": e.get("detail"), "drawdown_pct": e.get("drawdown_pct"),
            "balance_usd": e.get("balance_usd"),
        })

    items.sort(key=lambda i: i.get("ts") or 0, reverse=True)
    return {"items": items[:limit], "count": len(items[:limit])}


@app.get("/api/config")
def config() -> dict[str, Any]:
    """Full settings snapshot (no secrets — the private key is excluded by
    Settings' repr and never exported)."""
    try:
        from config.settings import settings as s
        names = [
            "PAPER_MODE", "MAX_POSITION_PCT", "DAILY_LOSS_HALT_PCT",
            "TOTAL_DRAWDOWN_KILL_PCT", "MAX_TOTAL_EXPOSURE_PCT",
            "EDGE_THRESHOLD_PCT", "MIN_CONFIDENCE", "MIN_MARKET_LIQUIDITY_USD",
            "MAX_DIRECTIONAL_ENTRY_PRICE", "TAKER_FEE_PCT",
            "CROSS_EXCHANGE_TOLERANCE_PCT", "SIMULATED_FILL_LATENCY_S",
            "MIN_ORDER_SIZE_USD", "TICK_SIZE", "TAKE_PROFIT_PCT",
            "EDGE_REVERSAL_EXIT_THRESHOLD_PCT", "SUM_TO_ONE_MIN_EDGE_PCT",
            "SUM_TO_ONE_MAX_POSITION_PCT", "MARKET_DISCOVERY_INTERVAL_S",
            "STARTING_PAPER_BALANCE_USD", "ASSUMED_ARBITRAGE_WINDOW_S",
            "PLATFORM_TAKER_DELAY_MS", "TELEGRAM_STATUS_INTERVAL_HOURS",
        ]
        out = {n: getattr(s, n, None) for n in names}
        return {"config": out, "ts": _now()}
    except Exception:
        return {"config": {}, "ts": _now()}


@app.get("/api/live")
def live() -> dict[str, Any]:
    state = _read_state()
    return {
        "ts": _now(),
        "state_age_s": _state_age_s(),
        "bot_status": "online" if _state_age_s() is not None and _state_age_s() < 15.0 else "offline",
        "state": state,
    }


@app.get("/api/markets")
def markets() -> dict[str, Any]:
    state = _read_state()
    rows = state.get("markets") or []
    now = _now()
    for m in rows:
        exp = m.get("expires_at_ts")
        m["time_remaining_s"] = max(0.0, exp - now) if exp else None
    return {"markets": rows, "count": len(rows)}


# --------------------------------------------------------------------------
# websocket
# --------------------------------------------------------------------------

@app.websocket("/ws/live")
async def ws_live(ws: WebSocket) -> None:
    """Push a full snapshot every ~2s. The sync read endpoints run inline
    (~30-60ms of SQLite) — fine at local scale; offload to a threadpool
    if this ever serves many clients."""
    await ws.accept()
    try:
        while True:
            payload = Snapshot(
                ts=_now(),
                overview=overview(),
                activity=activity(limit=40)["items"],
                positions=positions()["positions"],
                live={"state_age_s": _state_age_s()},
            )
            await ws.send_json(payload.model_dump())
            # Don't block on client pings — wait briefly, then push the next
            # snapshot on schedule. A dead client surfaces as a send failure.
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=1.0)
            except Exception:
                pass
            await asyncio.sleep(2.0)
    except Exception:
        pass


@app.get("/")
def index() -> dict[str, str]:
    return {
        "name": "Arbitrage Bot Command Center API",
        "docs": "/docs",
        "health": "/api/health",
    }
