"""
Live-refreshing terminal dashboard (rich). Runs as its own asyncio task so it
never blocks the trading loop; it only reads state, never mutates anything.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Optional

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

REFRESH_HZ = 1.5  # ~every 0.67s; spec asks for 1-2s, this sits comfortably inside that


@dataclass
class DashboardState:
    """Plain snapshot of everything the dashboard renders. Populated by main.py each tick."""
    mode: str = "PAPER"
    balance_usd: float = 0.0
    total_pnl_usd: float = 0.0
    win_rate_pct: float = 0.0
    open_positions: list[dict] = field(default_factory=list)
    last_trades: list[dict] = field(default_factory=list)  # most recent first, closed trades
    daily_halted: bool = False
    kill_switch_tripped: bool = False
    daily_pnl_pct: float = 0.0
    drawdown_pct: float = 0.0
    daily_halt_threshold_pct: float = 0.20
    kill_threshold_pct: float = 0.40
    # Feed liveness (engine/feed_health.FeedHealth): when either is False the
    # trading cycle is gated off, so the dashboard should say so loudly.
    binance_feed_healthy: bool = False
    polymarket_feed_healthy: bool = False


def _mode_panel(state: DashboardState) -> Panel:
    if state.mode == "LIVE":
        text = Text(" LIVE ", style="bold white on red")
    else:
        text = Text(" PAPER ", style="bold white on green")
    body = Group(
        text,
        Text(f"Balance: ${state.balance_usd:,.2f}", style="bold"),
        Text(
            f"Total PnL: ${state.total_pnl_usd:,.2f}",
            style="green" if state.total_pnl_usd >= 0 else "red",
        ),
        Text(f"Win rate: {state.win_rate_pct:.1f}%"),
    )
    return Panel(body, title="Mode & Account", border_style="cyan")


def _risk_panel(state: DashboardState) -> Panel:
    halt_text = Text(
        "HALTED (daily)" if state.daily_halted else "active",
        style="bold red" if state.daily_halted else "bold green",
    )
    kill_text = Text(
        "TRIPPED" if state.kill_switch_tripped else "active",
        style="bold red" if state.kill_switch_tripped else "bold green",
    )
    body = Group(
        Text.assemble("Daily halt: ", halt_text),
        Text(f"  Daily PnL: {state.daily_pnl_pct:.2%} (halts at -{state.daily_halt_threshold_pct:.0%})"),
        Text.assemble("Kill switch: ", kill_text),
        Text(f"  Drawdown: {state.drawdown_pct:.2%} (trips at -{state.kill_threshold_pct:.0%})"),
    )
    return Panel(body, title="Risk Manager", border_style="yellow")


def _feed_health_panel(state: DashboardState) -> Panel:
    """Feed liveness panel, mirroring the risk panel's layout. Each feed is
    shown as green 'healthy' or bold-red 'UNHEALTHY' — and the panel border
    goes red when either feed is down, since that means trading is gated off.
    Both feeds share one line so the panel stays compact now that the top
    row holds three panels."""
    binance_text = Text(
        "healthy" if state.binance_feed_healthy else "UNHEALTHY",
        style="bold green" if state.binance_feed_healthy else "bold red",
    )
    polymarket_text = Text(
        "healthy" if state.polymarket_feed_healthy else "UNHEALTHY",
        style="bold green" if state.polymarket_feed_healthy else "bold red",
    )
    body = Group(
        Text.assemble("Binance: ", binance_text, "  ", "Polymarket: ", polymarket_text),
    )
    border = "green" if state.binance_feed_healthy and state.polymarket_feed_healthy else "red"
    return Panel(body, title="Feed Health", border_style=border)


def _positions_table(state: DashboardState) -> Table:
    table = Table(title="Open Positions", expand=True)
    table.add_column("Market")
    table.add_column("Side")
    table.add_column("Entry")
    table.add_column("Size ($)", justify="right")
    if not state.open_positions:
        table.add_row("—", "—", "—", "—")
    for p in state.open_positions[:10]:
        table.add_row(
            str(p.get("market_id", ""))[:20],
            str(p.get("side", "")),
            f"{p.get('entry_price', 0):.3f}",
            f"{p.get('size_usd', 0):,.2f}",
        )
    return table


def _trades_table(state: DashboardState) -> Table:
    table = Table(title="Last 10 Trades", expand=True)
    table.add_column("Market")
    table.add_column("Side")
    table.add_column("Mode")
    table.add_column("PnL ($)", justify="right")
    if not state.last_trades:
        table.add_row("—", "—", "—", "—")
    for t in state.last_trades[:10]:
        pnl = t.get("realized_pnl_usd") or 0.0
        style = "green" if pnl >= 0 else "red"
        table.add_row(
            str(t.get("market_id", ""))[:20],
            str(t.get("side", "")),
            str(t.get("mode", "")),
            Text(f"{pnl:,.2f}", style=style),
        )
    return table


def render(state: DashboardState) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="top", size=8),
        Layout(name="middle"),
    )
    layout["top"].split_row(
        Layout(_mode_panel(state), name="mode"),
        Layout(_risk_panel(state), name="risk"),
        Layout(_feed_health_panel(state), name="feed_health"),
    )
    layout["middle"].split_row(
        Layout(Panel(_positions_table(state)), name="positions"),
        Layout(Panel(_trades_table(state)), name="trades"),
    )
    return layout


async def run_dashboard(get_state: Callable[[], DashboardState], console: Optional[Console] = None) -> None:
    """
    Long-running coroutine intended to be launched as its own asyncio.Task
    from main.py, e.g.:
        asyncio.create_task(run_dashboard(lambda: current_state))
    `get_state` is a zero-arg callable (or a lambda closing over shared state)
    returning the latest DashboardState snapshot — this keeps the dashboard
    fully decoupled from the trading loop's internals.
    """
    console = console or Console()
    with Live(render(get_state()), console=console, refresh_per_second=REFRESH_HZ, screen=False) as live:
        while True:
            await asyncio.sleep(1.0 / REFRESH_HZ)
            live.update(render(get_state()))
