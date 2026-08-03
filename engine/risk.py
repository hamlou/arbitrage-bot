"""
Risk manager: position sizing (half-Kelly, hard-capped), daily loss halt, and
a persistent total-drawdown kill switch.

All thresholds are read from Settings — nothing here is a magic number.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from alerts.telegram import AlertLevel, TelegramAlerter
from config.settings import Settings
from storage.db import Database

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SignalForSizing:
    """Minimal view of a signal needed for position sizing."""
    edge_pct: float       # e.g. 0.24 for a 24-point edge
    entry_price: float    # price of the side being bought, in (0, 1)


def _utc_day_key(ts: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%d")


class RiskManager:
    def __init__(self, settings: Settings, db: Database, alerter: TelegramAlerter):
        self.settings = settings
        self.db = db
        self.alerter = alerter

        self._peak_equity: Optional[float] = None
        self._day_start_equity: Optional[float] = None
        self._current_day_key: Optional[str] = None

        self._daily_halted = False
        self._kill_switch_tripped = False

    # -- Position sizing -----------------------------------------------------

    def position_size(self, signal: SignalForSizing, current_balance: float) -> float:
        """
        Fractional (half) Kelly, hard-capped at MAX_POSITION_PCT of current
        balance regardless of what Kelly suggests.

        Kelly fraction for a binary bet at price `p` (cost per $1 of payout)
        with edge `edge_pct`: f* = edge / odds, where odds = (1 - p) / p is the
        net-profit-to-stake ratio of a winning $1 payout bought at price p.
        We take half of that, per the spec.
        """
        if not (0 < signal.entry_price < 1):
            return 0.0
        if signal.edge_pct <= 0 or current_balance <= 0:
            return 0.0

        odds = (1 - signal.entry_price) / signal.entry_price
        if odds <= 0:
            return 0.0

        full_kelly_fraction = signal.edge_pct / odds
        half_kelly_fraction = 0.5 * full_kelly_fraction

        # Never negative, never above the hard cap.
        capped_fraction = max(0.0, min(half_kelly_fraction, self.settings.MAX_POSITION_PCT))
        return capped_fraction * current_balance

    # -- Lifecycle / state loading --------------------------------------------

    async def load_state(self, current_balance: float) -> None:
        """
        Call once at startup: restores kill-switch and daily-halt state from
        the DB so a restart can't silently clear either of them.
        """
        kill_row = await self.db.get_latest_risk_flag("KILL_SWITCH")
        reset_row = await self.db.get_latest_risk_flag("MANUAL_RESET")
        if kill_row and (not reset_row or reset_row["ts"] < kill_row["ts"]):
            self._kill_switch_tripped = True
            logger.warning("Kill switch is TRIPPED from a previous session — trading disabled until manual reset")

        halt_row = await self.db.get_latest_risk_flag("DAILY_HALT")
        today = _utc_day_key()
        if halt_row and _utc_day_key(halt_row["ts"]) == today:
            self._daily_halted = True
            logger.warning("Daily loss halt is ACTIVE from earlier today — no new trades until UTC midnight")

        self._current_day_key = today
        self._day_start_equity = current_balance
        self._peak_equity = current_balance

        # Recover peak equity from history, in case this isn't a fresh DB.
        curve = await self.db.get_equity_curve()
        if curve:
            historical_peak = max(row["balance_usd"] for row in curve)
            self._peak_equity = max(self._peak_equity, historical_peak)

    def is_trading_allowed(self) -> bool:
        return not self._daily_halted and not self._kill_switch_tripped

    @property
    def kill_switch_tripped(self) -> bool:
        return self._kill_switch_tripped

    @property
    def daily_halted(self) -> bool:
        return self._daily_halted

    # -- Per-update checks -----------------------------------------------------

    async def update(self, current_balance: float) -> None:
        """
        Call this after every balance-changing event (trade close, equity
        snapshot). Rolls the daily window at UTC midnight, and checks both the
        daily-loss and total-drawdown thresholds.
        """
        today = _utc_day_key()
        if today != self._current_day_key:
            # New UTC day: daily halt clears automatically. Kill switch does NOT.
            self._current_day_key = today
            self._day_start_equity = current_balance
            self._daily_halted = False
            logger.info("New UTC trading day — daily halt (if any) has been cleared")

        if self._peak_equity is None or current_balance > self._peak_equity:
            self._peak_equity = current_balance

        # -- Daily loss halt --
        if self._day_start_equity and self._day_start_equity > 0 and not self._daily_halted:
            daily_pnl_pct = (current_balance - self._day_start_equity) / self._day_start_equity
            if daily_pnl_pct <= -self.settings.DAILY_LOSS_HALT_PCT:
                self._daily_halted = True
                await self.db.log_risk_event(
                    event_type="DAILY_HALT",
                    detail=f"Daily PnL {daily_pnl_pct:.2%} breached -{self.settings.DAILY_LOSS_HALT_PCT:.0%} halt threshold",
                    balance_usd=current_balance,
                    drawdown_pct=daily_pnl_pct,
                )
                await self.alerter.send_alert(
                    f"Daily loss halt triggered. Daily PnL {daily_pnl_pct:.2%}. "
                    f"No new trades until UTC midnight. Manual restart required.",
                    level=AlertLevel.WARNING,
                )
                logger.warning("DAILY LOSS HALT triggered at %.2f%% PnL", daily_pnl_pct * 100)

        # -- Total drawdown kill switch (persistent) --
        if self._peak_equity and self._peak_equity > 0 and not self._kill_switch_tripped:
            drawdown_pct = (self._peak_equity - current_balance) / self._peak_equity
            if drawdown_pct >= self.settings.TOTAL_DRAWDOWN_KILL_PCT:
                self._kill_switch_tripped = True
                await self.db.log_risk_event(
                    event_type="KILL_SWITCH",
                    detail=f"Drawdown {drawdown_pct:.2%} from peak {self._peak_equity:.2f} breached "
                           f"-{self.settings.TOTAL_DRAWDOWN_KILL_PCT:.0%} kill-switch threshold",
                    balance_usd=current_balance,
                    drawdown_pct=drawdown_pct,
                )
                await self.alerter.send_alert(
                    f"KILL SWITCH TRIPPED. Drawdown {drawdown_pct:.2%} from peak. "
                    f"Trading halted indefinitely — requires manual DB reset after review.",
                    level=AlertLevel.CRITICAL,
                )
                logger.error("KILL SWITCH TRIPPED at %.2f%% drawdown from peak", drawdown_pct * 100)

    async def manual_reset_kill_switch(self, operator_note: str) -> None:
        """
        The only way the kill switch clears. Deliberately requires an explicit
        call (e.g. from a CLI/admin script) with a human-written note — this
        is not something main.py should ever call automatically.
        """
        self._kill_switch_tripped = False
        self._peak_equity = None  # will be re-seeded from current balance on next update()
        await self.db.log_risk_event(
            event_type="MANUAL_RESET",
            detail=f"Kill switch manually reset by operator: {operator_note}",
        )
        await self.alerter.send_alert(
            f"Kill switch manually reset. Operator note: {operator_note}",
            level=AlertLevel.WARNING,
        )
        logger.warning("Kill switch manually reset: %s", operator_note)
