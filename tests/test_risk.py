"""
Full unit tests for engine/risk.py, covering:
- normal Kelly sizing
- hard cap triggering
- daily halt triggering, and persisting through a simulated restart
- kill switch triggering, and persisting through a simulated restart
"""
import pytest

from alerts.telegram import TelegramAlerter
from config.settings import Settings
from engine.risk import RiskManager, SignalForSizing
from storage.db import Database


def make_settings(**overrides) -> Settings:
    defaults = dict(
        MAX_POSITION_PCT=0.08,
        DAILY_LOSS_HALT_PCT=0.20,
        TOTAL_DRAWDOWN_KILL_PCT=0.40,
        EDGE_THRESHOLD_PCT=0.05,
        MIN_CONFIDENCE=0.85,
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def make_alerter() -> TelegramAlerter:
    return TelegramAlerter(bot_token=None, chat_id=None)  # no-op, logs locally only


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    yield database
    await database.close()


# -- Position sizing -----------------------------------------------------------

async def test_normal_kelly_sizing_is_between_zero_and_cap(db):
    settings = make_settings()
    risk = RiskManager(settings, db, make_alerter())
    await risk.load_state(current_balance=1000)

    signal = SignalForSizing(edge_pct=0.10, entry_price=0.5)
    size = risk.position_size(signal, current_balance=1000)

    assert 0 < size <= settings.MAX_POSITION_PCT * 1000


async def test_zero_edge_produces_zero_size(db):
    settings = make_settings()
    risk = RiskManager(settings, db, make_alerter())
    await risk.load_state(current_balance=1000)

    signal = SignalForSizing(edge_pct=0.0, entry_price=0.5)
    assert risk.position_size(signal, current_balance=1000) == 0.0


async def test_hard_cap_triggers_on_large_edge(db):
    """A very large edge should push half-Kelly above MAX_POSITION_PCT, and the
    cap must clamp it exactly at that ceiling."""
    settings = make_settings(MAX_POSITION_PCT=0.08)
    risk = RiskManager(settings, db, make_alerter())
    await risk.load_state(current_balance=1000)

    signal = SignalForSizing(edge_pct=0.45, entry_price=0.5)  # odds=1.0, half-Kelly=0.225 >> cap
    size = risk.position_size(signal, current_balance=1000)

    assert size == pytest.approx(settings.MAX_POSITION_PCT * 1000)


# -- Daily halt: trigger + persistence through restart ----------------------------

async def test_daily_halt_triggers_on_breach(db):
    settings = make_settings(DAILY_LOSS_HALT_PCT=0.20)
    risk = RiskManager(settings, db, make_alerter())
    await risk.load_state(current_balance=1000)

    assert risk.is_trading_allowed()
    await risk.update(current_balance=1000)  # baseline, no change
    await risk.update(current_balance=750)   # -25% breaches -20% halt threshold

    assert risk.daily_halted is True
    assert risk.is_trading_allowed() is False


async def test_daily_halt_persists_through_restart(tmp_path):
    db_path = str(tmp_path / "persist.db")
    settings = make_settings(DAILY_LOSS_HALT_PCT=0.20)

    # -- "session 1": trigger the halt --
    db1 = Database(db_path)
    await db1.connect()
    risk1 = RiskManager(settings, db1, make_alerter())
    await risk1.load_state(current_balance=1000)
    await risk1.update(current_balance=1000)
    await risk1.update(current_balance=700)  # -30%, breaches halt
    assert risk1.daily_halted is True
    await db1.close()

    # -- "session 2": simulate a restart, same UTC day --
    db2 = Database(db_path)
    await db2.connect()
    risk2 = RiskManager(settings, db2, make_alerter())
    await risk2.load_state(current_balance=700)

    assert risk2.daily_halted is True
    assert risk2.is_trading_allowed() is False
    await db2.close()


# -- Kill switch: trigger + persistence through restart -----------------------------

async def test_kill_switch_triggers_on_total_drawdown_breach(db):
    settings = make_settings(TOTAL_DRAWDOWN_KILL_PCT=0.40)
    risk = RiskManager(settings, db, make_alerter())
    await risk.load_state(current_balance=1000)

    await risk.update(current_balance=1000)  # peak = 1000
    await risk.update(current_balance=550)   # -45% from peak, breaches -40% kill threshold

    assert risk.kill_switch_tripped is True
    assert risk.is_trading_allowed() is False


async def test_kill_switch_does_not_clear_on_new_day(db):
    """Unlike the daily halt, the kill switch must NOT auto-clear when the
    UTC day rolls over."""
    settings = make_settings(TOTAL_DRAWDOWN_KILL_PCT=0.40)
    risk = RiskManager(settings, db, make_alerter())
    await risk.load_state(current_balance=1000)

    await risk.update(current_balance=1000)
    await risk.update(current_balance=500)  # trips kill switch
    assert risk.kill_switch_tripped is True

    # Force the tracker's day key to simulate a UTC-midnight rollover.
    risk._current_day_key = "2000-01-01"
    await risk.update(current_balance=500)

    assert risk.kill_switch_tripped is True  # still tripped
    assert risk.daily_halted is False        # daily halt did roll over/clear


async def test_kill_switch_persists_through_restart(tmp_path):
    db_path = str(tmp_path / "persist_kill.db")
    settings = make_settings(TOTAL_DRAWDOWN_KILL_PCT=0.40)

    db1 = Database(db_path)
    await db1.connect()
    risk1 = RiskManager(settings, db1, make_alerter())
    await risk1.load_state(current_balance=1000)
    await risk1.update(current_balance=1000)
    await risk1.update(current_balance=500)  # trips kill switch
    assert risk1.kill_switch_tripped is True
    await db1.close()

    db2 = Database(db_path)
    await db2.connect()
    risk2 = RiskManager(settings, db2, make_alerter())
    await risk2.load_state(current_balance=500)

    assert risk2.kill_switch_tripped is True
    assert risk2.is_trading_allowed() is False
    await db2.close()


async def test_kill_switch_requires_explicit_manual_reset(tmp_path):
    db_path = str(tmp_path / "reset.db")
    settings = make_settings(TOTAL_DRAWDOWN_KILL_PCT=0.40)

    database = Database(db_path)
    await database.connect()
    risk = RiskManager(settings, database, make_alerter())
    await risk.load_state(current_balance=1000)
    await risk.update(current_balance=1000)
    await risk.update(current_balance=500)
    assert risk.kill_switch_tripped is True

    await risk.manual_reset_kill_switch(operator_note="reviewed logs, resuming")
    assert risk.kill_switch_tripped is False
    # The same -50% move also breached the daily halt threshold, and that's a
    # separate, independent flag — resetting the kill switch does not clear it.
    assert risk.daily_halted is True
    assert risk.is_trading_allowed() is False

    # Only once the daily halt also clears (e.g. new UTC day) does trading resume.
    risk._current_day_key = "2000-01-01"
    await risk.update(current_balance=500)
    assert risk.daily_halted is False
    assert risk.is_trading_allowed() is True
    await database.close()
