"""
Build the clean status/stats digest shown in Telegram (both the periodic
push and the /status /stats commands). Pure formatting — no I/O, no network,
no DB access — so it's trivially unit-testable and the caller decides what
data to feed it (see TelegramReporter and TradingApp._build_status_snapshot).
"""
from __future__ import annotations

from typing import Any, Optional

WIDTH = 46


def _line(key: str, value: Any, indent: int = 0) -> str:
    pad = "  " * indent
    return f"{pad}{key:<18}: {value}"


def _fmt_money(v: Optional[float], signed: bool = False) -> str:
    """Format a dollar value. signed=True prefixes a '+' for positive values
    (used for PnL, where the sign is the point); plain values (balance,
    equity, sizes) stay sign-less."""
    if v is None:
        return "n/a"
    sign = "+" if signed and v > 0 else ""
    return f"{sign}${v:,.2f}"


def _feed_marker(ok: Optional[bool]) -> str:
    if ok is None:
        return "?"
    return "OK" if ok else "DOWN"


def _mode_label(mode: str) -> str:
    m = (mode or "?").upper()
    if m == "PAPER":
        return "PAPER (demo)"
    if m == "LIVE":
        return "LIVE (real)"
    return m


def _html_escape(value: Any) -> str:
    """Escape text for Telegram's parse_mode="HTML". Telegram HTML supports a
    small tag set (<b>, <code>, <pre>, ...) and requires & < > to be escaped.
    Everything user/DB-derived must pass through here before embedding."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _html_line(key: str, value: str) -> str:
    return f"<b>{_html_escape(key)}</b>: {value}"


def _num(v, default, fmt: str) -> str:
    """None-safe number formatting. A key that exists with value None (e.g.
    latencies before any trade has fired) must render as the default instead
    of crashing on '{None:.0f}' — the /latency button was silent for exactly
    this reason (2026-08-09)."""
    return f"{v:{fmt}}" if isinstance(v, (int, float)) else f"{default:{fmt}}"


def _html_money(v: Optional[float], signed: bool = False) -> str:
    return f"<code>{_fmt_money(v, signed=signed)}</code>"


def _html_feed_marker(ok: Optional[bool]) -> str:
    # Telegram HTML supports only <b>/<i>/<u>/<s>/<a>/<code>/<pre> — no spans.
    if ok is None:
        return "<code>?</code>"
    return "<b>OK</b>" if ok else "<b>DOWN</b>"


def _html_risk_flag(tripped: bool, label: str) -> str:
    if tripped:
        return f"<b>{_html_escape(label)}</b>"
    return f"<code>{_html_escape(label)}</code>"


def _header(mode: str) -> str:
    # ASCII-only separator (the em-dash mangled on Windows consoles) — the
    # header and every value below are console-safe.
    return f"{'=' * WIDTH}\n  Polymarket Arb Bot - {_mode_label(mode)}\n{'=' * WIDTH}"


def _account_section(s: dict) -> list[str]:
    win_rate = s.get("win_rate_pct")
    wr = f"{win_rate:.1f}%" if win_rate is not None else "n/a"
    return [
        "",
        "-- ACCOUNT --",
        _line("Balance", _fmt_money(s.get("balance_usd"))),
        _line("Equity", _fmt_money(s.get("equity_usd", s.get("balance_usd")))),
        _line("Total PnL", _fmt_money(s.get("total_pnl_usd"), signed=True)),
        _line("Win rate", wr),
        _line("Closed trades", s.get("closed_trades", 0)),
        _line("Open positions", s.get("open_positions", 0)),
    ]


def _system_section(s: dict) -> list[str]:
    return [
        "",
        "-- SYSTEM --",
        _line("Uptime", s.get("uptime") or "n/a"),
        _line("Trading", "PAUSED" if s.get("paused") else "active"),
        _line("Alerts", "MUTED" if s.get("alerts_muted") else "on"),
        _line("Binance feed", _feed_marker(s.get("binance_feed_healthy"))),
        _line("Polymarket feed", _feed_marker(s.get("polymarket_feed_healthy"))),
        _line("Daily halt", "HALTED" if s.get("daily_halted") else "active"),
        _line("Kill switch", "TRIPPED" if s.get("kill_switch_tripped") else "active"),
    ]


def _positions_section(s: dict) -> list[str]:
    positions = s.get("positions") or []
    lines = ["", "-- OPEN POSITIONS --"]
    if not positions:
        lines.append(_line("(none)", ""))
        return lines
    for p in positions[:8]:
        market = str(p.get("market_id", "?"))[:20]
        side = str(p.get("side", "?"))
        size = _fmt_money(p.get("size_usd"))
        price = p.get("entry_price")
        price_s = f"{price:.3f}" if price is not None else "?"
        lines.append(_line(market, f"{side} {size} @ {price_s}"))
    return lines


def _recent_section(s: dict) -> list[str]:
    trades = s.get("recent_trades") or []
    lines = ["", "-- RECENT TRADES --"]
    if not trades:
        lines.append(_line("(none yet)", ""))
        return lines
    for t in trades[:6]:
        market = str(t.get("market_id", "?"))[:16]
        side = str(t.get("side", "?"))
        pnl = _fmt_money(t.get("realized_pnl_usd"), signed=True)
        reason = str(t.get("exit_reason") or t.get("status") or "")
        lines.append(_line(market, f"{side}  PnL {pnl}  ({reason})"))
    return lines


def _strategy_section(s: dict) -> list[str]:
    by_strategy = s.get("by_strategy") or {}
    lines = ["", "-- BY STRATEGY --"]
    if not by_strategy:
        lines.append(_line("(no closed trades)", ""))
        return lines
    for name, stats in sorted(by_strategy.items()):
        n = stats.get("trades", 0)
        pnl = _fmt_money(stats.get("pnl_usd"), signed=True)
        wr = stats.get("win_rate_pct")
        wr_s = f"{wr:.0f}%" if wr is not None else "n/a"
        lines.append(_line(str(name), f"{n} trades, PnL {pnl}, WR {wr_s}"))
    return lines


def format_status_report(s: dict) -> str:
    """
    Render a status snapshot dict into the plain-text digest shown on
    Telegram. Keys are all optional — the formatter never raises on a
    missing field. Expected keys (produced by TradingApp._build_status_snapshot):
        mode, balance_usd, equity_usd, total_pnl_usd, win_rate_pct,
        closed_trades, open_positions, uptime, binance_feed_healthy,
        polymarket_feed_healthy, daily_halted, kill_switch_tripped, paused,
        alerts_muted, positions, recent_trades, by_strategy
    """
    note = s.get("stats_note")
    note_lines = ([f"NOTE: {note}", ""] if note else [])

    sections = (
        [_header(s.get("mode", "?"))]
        + note_lines
        + _account_section(s)
        + _system_section(s)
        + _positions_section(s)
        + _recent_section(s)
        + _strategy_section(s)
    )
    return "\n".join(sections)


# ---------------------------------------------------------------------------
# HTML CRM formatters (parse_mode="HTML") — used by the /crm /positions
# /trades /risk /feeds /config /latency commands. Same input snapshot dict as
# the plain formatter, plus optional feed_detail / risk_detail / config /
# latency sub-dicts produced by TradingApp._build_status_snapshot.
# ---------------------------------------------------------------------------


def format_crm_html(s: dict) -> str:
    """
    The full CRM dashboard as an HTML message: account, system, feed health
    with reconnect/staleness detail, risk, open positions, recent trades,
    per-strategy breakdown, latency vs window, and key config. Missing keys
    are rendered as "n/a" — never raises.
    """
    note = s.get("stats_note")
    note_lines = [f"<i>NOTE: {_html_escape(note)}</i>", ""] if note else []

    sections = [
        "<b>\U0001F680 Polymarket Arb Bot — {}</b>".format(_html_escape(_mode_label(s.get("mode", "?")))),
        *note_lines,
        "<b>\U0001F4B0 ACCOUNT</b>",
        _html_line("Balance", _html_money(s.get("balance_usd"))),
        _html_line("Equity", _html_money(s.get("equity_usd", s.get("balance_usd")))),
        _html_line("Total PnL", _html_money(s.get("total_pnl_usd"), signed=True)),
        _html_line(
            "Win rate",
            f"<code>{s['win_rate_pct']:.1f}%</code>" if s.get("win_rate_pct") is not None else "<code>n/a</code>",
        ),
        _html_line("Closed / open", f"<code>{s.get('closed_trades', 0)}</code> / <code>{s.get('open_positions', 0)}</code>"),
        "",
        "<b>\U0001F527 SYSTEM</b>",
        _html_line("Uptime", f"<code>{_html_escape(s.get('uptime') or 'n/a')}</code>"),
        _html_line("Trading", "<b>PAUSED</b>" if s.get("paused") else "<code>active</code>"),
        _html_line("Alerts", "<b>MUTED</b>" if s.get("alerts_muted") else "<code>on</code>"),
        _html_line("Daily halt", _html_risk_flag(s.get("daily_halted"), "HALTED") if s.get("daily_halted") else "<code>active</code>"),
        _html_line("Kill switch", _html_risk_flag(s.get("kill_switch_tripped"), "TRIPPED") if s.get("kill_switch_tripped") else "<code>active</code>"),
        "",
        "<b>\U0001F4C1 FEED HEALTH</b>",
        *_html_feed_detail_lines(s),
        "",
        "<b>\U0001F4C8 RISK</b>",
        *_html_risk_detail_lines(s),
        "",
        "<b>\U0001F6D2 OPEN POSITIONS</b>",
        *_html_positions_lines(s),
        "",
        "<b>\U0001F4C4 RECENT TRADES</b>",
        *_html_trades_lines(s),
        "",
        "<b>\U0001F3AF BY STRATEGY</b>",
        *_html_strategy_lines(s),
        "",
        "<b>\U000026A1 LATENCY</b>",
        *_html_latency_lines(s),
    ]
    return "\n".join(sections)


def _html_feed_detail_lines(s: dict) -> list[str]:
    fd = s.get("feed_detail") or {}
    lines: list[str] = []
    for feed in ("binance", "polymarket"):
        info = fd.get(feed) or {}
        ok = s.get(f"{feed}_feed_healthy")
        reconnects = info.get("reconnects_10m")
        stale = info.get("stale_s")
        parts = ["Binance" if feed == "binance" else "Polymarket", _html_feed_marker(ok)]
        if reconnects is not None:
            parts.append(f"reconnects: <code>{reconnects}</code>/10m")
        if stale is not None:
            parts.append(f"last msg: <code>{stale:.0f}s</code> ago")
        lines.append("  ".join(parts))
    if not lines:
        lines.append("<code>n/a</code>")
    return lines


def _html_risk_detail_lines(s: dict) -> list[str]:
    rd = s.get("risk_detail") or {}
    if not rd:
        return ["<code>n/a</code>"]
    lines = [
        _html_line(
            "Daily PnL",
            f"<code>{_num(rd.get('daily_pnl_pct'), 0.0, '+.2%')}</code> "
            f"(halt at {_num(rd.get('daily_halt_threshold_pct'), 0.20, '.0%')})",
        ),
        _html_line(
            "Drawdown",
            f"<code>{_num(rd.get('drawdown_pct'), 0.0, '.2%')}</code> "
            f"(kill at {_num(rd.get('kill_threshold_pct'), 0.40, '.0%')})",
        ),
        _html_line("Daily halt", "<b>HALTED</b>" if s.get("daily_halted") else "<code>active</code>"),
        _html_line("Kill switch", "<b>TRIPPED</b>" if s.get("kill_switch_tripped") else "<code>active</code>"),
    ]
    return lines


def _html_positions_lines(s: dict) -> list[str]:
    positions = s.get("positions") or []
    if not positions:
        return ["<code>(none)</code>"]
    lines: list[str] = []
    for p in positions[:8]:
        market = _html_escape(str(p.get("market_id", "?"))[:20])
        side = _html_escape(str(p.get("side", "?")))
        size = _fmt_money(p.get("size_usd"))
        price = p.get("entry_price")
        price_s = f"{price:.3f}" if price is not None else "?"
        lines.append(f"<code>{market}</code> {side} {size} @ {price_s}")
    return lines


def _html_trades_lines(s: dict) -> list[str]:
    trades = s.get("recent_trades") or []
    if not trades:
        return ["<code>(none yet)</code>"]
    lines: list[str] = []
    for t in trades[:6]:
        market = _html_escape(str(t.get("market_id", "?"))[:16])
        side = _html_escape(str(t.get("side", "?")))
        pnl = _fmt_money(t.get("realized_pnl_usd"), signed=True)
        reason = _html_escape(str(t.get("exit_reason") or t.get("status") or ""))
        lines.append(f"<code>{market}</code> {side} PnL {pnl} ({reason})")
    return lines


def _html_strategy_lines(s: dict) -> list[str]:
    by_strategy = s.get("by_strategy") or {}
    if not by_strategy:
        return ["<code>(no closed trades)</code>"]
    lines: list[str] = []
    for name, stats in sorted(by_strategy.items()):
        n = stats.get("trades", 0)
        pnl = _fmt_money(stats.get("pnl_usd"), signed=True)
        wr = stats.get("win_rate_pct")
        wr_s = f"{wr:.0f}%" if wr is not None else "n/a"
        lines.append(f"<code>{_html_escape(str(name))}</code> {n} trades, PnL {pnl}, WR {wr_s}")
    return lines


def _html_latency_lines(s: dict) -> list[str]:
    lat = s.get("latency") or {}
    if not lat:
        return ["<code>no latency data yet — run the bot for a while</code>"]
    lines = [
        _html_line(
            "Tick->signal (p50/p95)",
            f"<code>{_num(lat.get('tick_to_signal_p50_ms'), 0.0, '.0f')} / "
            f"{_num(lat.get('tick_to_signal_p95_ms'), 0.0, '.0f')} ms</code>",
        ),
        _html_line(
            "Tick->order (p50/p95)",
            f"<code>{_num(lat.get('tick_to_order_p50_ms'), 0.0, '.0f')} / "
            f"{_num(lat.get('tick_to_order_p95_ms'), 0.0, '.0f')} ms</code>",
        ),
        _html_line(
            "Platform delay",
            f"<code>{_num(lat.get('platform_delay_ms'), 250.0, '.0f')} ms</code> "
            "(CLOB taker-order delay)",
        ),
        _html_line(
            "Window",
            f"<code>{_num(lat.get('window_s'), 2.0, '.1f')} s</code> — "
            f"verdict: {_html_escape(lat.get('verdict', 'n/a'))}",
        ),
    ]
    return lines


# Individual section formatters (the /positions /trades /risk /feeds /config
# /latency commands reuse the same section builders as the full CRM).


def format_positions_html(s: dict) -> str:
    return "<b>OPEN POSITIONS</b>\n" + "\n".join(_html_positions_lines(s))


def format_trades_html(s: dict) -> str:
    return "<b>RECENT TRADES</b>\n" + "\n".join(_html_trades_lines(s))


def format_risk_html(s: dict) -> str:
    return "<b>RISK</b>\n" + "\n".join(_html_risk_detail_lines(s))


def format_feeds_html(s: dict) -> str:
    return "<b>FEED HEALTH</b>\n" + "\n".join(_html_feed_detail_lines(s))


def format_config_html(s: dict) -> str:
    cfg = s.get("config") or {}
    if not cfg:
        return "<b>CONFIG</b>\n<code>n/a</code>"
    lines = ["<b>CONFIG</b>"]
    for key in sorted(cfg.keys()):
        lines.append(_html_line(key, f"<code>{_html_escape(cfg[key])}</code>"))
    return "\n".join(lines)


def format_latency_html(s: dict) -> str:
    return "<b>LATENCY</b>\n" + "\n".join(_html_latency_lines(s))


def format_forensics_digest(summary: dict) -> str:
    """
    Plain-text daily forensics digest (the /forensics content, also pushed
    automatically once a day). Pure formatting of the summary dict built by
    engine/exit_forensics.build_digest_summary — this closes the loop on the
    freeze rule: the premature-vs-protective split and the run-progress gates
    are pushed automatically instead of requiring a human to run
    scripts/analyze_exits.py by hand.
    """
    n = summary.get("closed_trades", 0)
    days = summary.get("days_elapsed", 0.0)
    distinct = summary.get("distinct_trading_days", 0)
    lines = [
        "🔬 EXIT FORENSICS (daily digest)",
        "=" * 30,
        f"Closed trades      : {n}",
        f"Days elapsed        : {days:.1f}",
        f"Distinct trade days : {distinct}",
        f"Net PnL             : {_fmt_money(summary.get('net_pnl_usd'), signed=True)}",
        "",
        "Per exit reason (net PnL):",
    ]
    by_reason = summary.get("by_reason") or {}
    for reason, pnl in by_reason.items():
        lines.append(f"  {reason:<14} {_fmt_money(pnl, signed=True)}")

    rev_n = summary.get("reversals_n", 0)
    prem_n = summary.get("premature_n", 0)
    held_n = summary.get("held_won_n", 0)
    prot_n = summary.get("protective_n", 0)
    lines += [
        "",
        "EDGE_REVERSAL exits — premature vs protective:",
        f"  PREMATURE (market hit target after we left): {prem_n} trades "
        f"({_fmt_money(summary.get('premature_dollars'), signed=True)} of "
        f"{_fmt_money(summary.get('reversal_dollars'), signed=True)} reversal loss)",
        f"  Held side WON at settlement (cut too early):  {held_n}",
        f"  Protective (kept falling):                    {prot_n}",
        f"  No probe data yet:                            {summary.get('no_data_n', 0)}",
        f"  Total reversals:                              {rev_n}",
        "",
        "Freeze gate (thresholds unlock at):",
        f"  Trades: {n} / {summary.get('freeze_min_trades', 100)}",
        f"  Days:   {days:.1f} / {summary.get('freeze_min_days', 7.0)}",
        "",
        "Live-trading gate (validate_paper_run.py):",
        f"  Trades: {n} / {summary.get('live_min_trades', 200)}",
        f"  Days:   {days:.1f} / {summary.get('live_min_days', 7.0)}",
        f"  Distinct days: {distinct} / {summary.get('live_min_distinct_days', 5)}",
        "",
        "Measurement only — no thresholds changed.",
    ]
    return "\n".join(lines)
