"""
Central configuration for polymarket-arb-bot.

Design intent: this file is the single source of truth for every threshold and
flag that affects risk. Nothing outside this module should hardcode a magic
number for position sizing, drawdown limits, or the live-trading gate.
"""
from __future__ import annotations

from typing import Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Mode -----------------------------------------------------------
    PAPER_MODE: bool = True

    # --- Live trading gate (see engine/broker_live.py) -------------------
    # Three independent flags. All three, plus PAPER_MODE=False, are required
    # before build_live_broker() will ever instantiate a real broker.
    LIVE_TRADING_CONFIRMED_1: bool = False
    LIVE_TRADING_CONFIRMED_2: bool = False
    LIVE_TRADING_CONFIRMED_3: bool = False

    # --- Risk parameters --------------------------------------------------
    MAX_POSITION_PCT: float = 0.08
    DAILY_LOSS_HALT_PCT: float = 0.20
    TOTAL_DRAWDOWN_KILL_PCT: float = 0.40
    # Total notional across ALL simultaneously open positions, as a fraction of
    # equity — separate from the per-trade MAX_POSITION_PCT cap. Without this,
    # a bot that's right about MAX_POSITION_PCT on every individual trade can
    # still stack enough concurrent positions to have far more than 8% of
    # equity at risk at once.
    MAX_TOTAL_EXPOSURE_PCT: float = 0.30

    # --- Signal thresholds --------------------------------------------------
    EDGE_THRESHOLD_PCT: float = 0.05
    MIN_CONFIDENCE: float = 0.85
    MIN_MARKET_LIQUIDITY_USD: float = 50_000.0
    # Cross-exchange sanity gate: before firing a signal, the latest known
    # Binance and Coinbase prices for the asset must agree within this many
    # percent (0.1 = 0.1%). A bigger divergence usually means one feed is
    # stale or misbehaving — don't trade on it. The check is skipped (signal
    # allowed) when either source hasn't delivered a tick yet, since the bot
    # has always been able to run on Binance alone.
    CROSS_EXCHANGE_TOLERANCE_PCT: float = 0.1

    # --- Fill realism (paper mode) -------------------------------------------
    # Simulates the delay between "we decided to trade" and "the order actually
    # lands," by re-consulting the (real, continuously-updating) WS book after
    # this delay instead of filling instantly against the book we evaluated on.
    SIMULATED_FILL_LATENCY_S: float = 0.3
    MIN_ORDER_SIZE_USD: float = 1.0
    TICK_SIZE: float = 0.01

    # --- Exit logic -----------------------------------------------------
    # Early take-profit: exit if the position's current mark-to-market value
    # has gained at least this fraction of stake before contract expiry.
    TAKE_PROFIT_PCT: float = 0.5
    # Exit early if the live edge (recomputed each cycle) has flipped against
    # the position's original side by more than this — i.e. our own model no
    # longer agrees with the trade we're holding.
    EDGE_REVERSAL_EXIT_THRESHOLD_PCT: float = 0.10

    # --- Sum-to-one (combo) arbitrage -----------------------------------------
    # If YES_ask + NO_ask (net of modeled fees) is under $1 by at least this
    # much, buying both sides locks in a risk-free profit at settlement
    # regardless of outcome — this doesn't depend on directional forecasting
    # at all, so it gets its own threshold and its own position cap rather
    # than sharing Kelly sizing with the directional strategy.
    SUM_TO_ONE_MIN_EDGE_PCT: float = 0.01
    SUM_TO_ONE_MAX_POSITION_PCT: float = 0.10

    # --- Market discovery -----------------------------------------------
    # Decoupled from the (much faster) per-cycle signal evaluation loop — no
    # reason to re-hit Gamma every second just to catch new 5/15-min markets.
    MARKET_DISCOVERY_INTERVAL_S: float = 3.0

    # --- Paper trading ----------------------------------------------------
    STARTING_PAPER_BALANCE_USD: float = 1000.0

    # --- Telegram -----------------------------------------------------------
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_CHAT_ID: Optional[str] = None

    # --- Live credentials ---------------------------------------------------
    # Deliberately: no default, never logged, never printed, never included in
    # __repr__. Pydantic's default repr would print field values, so we
    # override __repr__ / __str__ below to redact this specific field.
    POLYGON_PRIVATE_KEY: Optional[str] = Field(default=None, repr=False, exclude=True)

    # --- Storage --------------------------------------------------------
    DATABASE_PATH: str = "storage/arb_bot.db"

    # --- Infra -----------------------------------------------------------
    # Deliberately no default — see scripts/benchmark_rpc.py. Pick this based
    # on a measurement from wherever the bot actually runs, not a guess.
    POLYGON_RPC_URL: Optional[str] = None

    # --- Validators ----------------------------------------------------------
    @field_validator(
        "MAX_POSITION_PCT",
        "DAILY_LOSS_HALT_PCT",
        "TOTAL_DRAWDOWN_KILL_PCT",
        "MAX_TOTAL_EXPOSURE_PCT",
        "SUM_TO_ONE_MAX_POSITION_PCT",
        "EDGE_REVERSAL_EXIT_THRESHOLD_PCT",
        "EDGE_THRESHOLD_PCT",
        "MIN_CONFIDENCE",
    )
    @classmethod
    def _must_be_fraction(cls, v: float, info) -> float:
        if not (0 < v <= 1):
            raise ValueError(f"{info.field_name} must be in (0, 1], got {v}")
        return v

    @model_validator(mode="after")
    def _fail_loudly_if_key_present_in_paper_mode(self) -> "Settings":
        """
        Hard safety rail: a real private key has no business being loaded
        while we believe we're in a zero-risk paper run. This is intentionally
        a hard crash, not a warning — silent misconfiguration here is exactly
        the failure mode that costs people money.
        """
        if self.PAPER_MODE and self.POLYGON_PRIVATE_KEY:
            raise RuntimeError(
                "SAFETY HALT: POLYGON_PRIVATE_KEY is set while PAPER_MODE=True. "
                "This should never happen — paper mode must never have access to "
                "a real wallet key. Remove POLYGON_PRIVATE_KEY from your "
                "environment/.env, or set PAPER_MODE=False only once you have "
                "deliberately decided to go live (see engine/broker_live.py)."
            )
        return self

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "Settings(...redacted...)"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.__repr__()


# Module-level singleton, imported everywhere else as `from config.settings import settings`
settings = Settings()
