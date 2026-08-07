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
    MIN_MARKET_LIQUIDITY_USD: float = 1_000.0

    # --- Entry discipline (directional strategy) ---------------------------
    # Never buy a directional token above this price. Buying at 0.82 means
    # being right 82% of the time just to break even before fees — a
    # miscalibrated model read on a 5-minute window deserves no such
    # assumption. Verified 2026-08-06: the bot opened YES @ 0.82 and NO @ 0.99
    # (the latter can mathematically never profit after fees) and lost ~$170
    # on two of those entries. 0.80 is deliberately conservative.
    MAX_DIRECTIONAL_ENTRY_PRICE: float = 0.80
    # Taker fee used to make the edge gate fee-aware (matches
    # broker_paper.DEFAULT_FEE_PCT). A raw edge that doesn't clear the
    # round-trip fee isn't an edge.
    TAKER_FEE_PCT: float = 0.02
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

    # --- Entry timing guards (only trade a REAL lag, never a drift) -------
    # The strategy's premise is "Polymarket LAGS a fresh Binance move." An
    # edge is only real while there IS a move for the market to lag. Verified
    # 2026-08-07: the model bought NO @ 0.69 (implied 0.86) while BTC was
    # actually rising and Polymarket held YES at 0.30-0.33 — the model was
    # fading a move with a stale reference price, and the position went to
    # ~$0 in 5s (-$35). Two guards encode the premise:
    #   FRESH_MOVE: the model's direction must agree with the asset's actual
    #     price movement over the last FRESH_MOVE_LOOKBACK_S seconds, by at
    #     least FRESH_MOVE_MIN_PCT. No fresh aligned move -> no lag to
    #     front-run -> no trade.
    #   MIN_ENTRY_TIME_REMAINING_S: never ENTER a directional trade in the
    #     final stretch of a window — the market has effectively decided, and
    #     book noise dominates.
    FRESH_MOVE_LOOKBACK_S: float = 15.0
    FRESH_MOVE_MIN_PCT: float = 0.0006   # 0.06% in 15s, aligned with the model
    MIN_ENTRY_TIME_REMAINING_S: float = 45.0
    # Reference-price trust guard for the fair-value model (verified
    # 2026-08-07): the reference price is captured at FIRST SIGHTING of a
    # market. If discovery catches a market late (bot restart mid-contract,
    # or Gamma serving it late), the "reference" is really the CURRENT price
    # — so the fair-value z-score compares price-to-itself and points the
    # WRONG way, producing saturated reads like "model 2% vs market 99.5%"
    # (836 such blocked signals in one run). Only trust a first-sighting
    # reference when at least this fraction of the window remains at capture;
    # otherwise leave reference_price unset so fair-value stays off and the
    # calibrated momentum fallback (honestly ~52%) refuses to invent an edge.
    REFERENCE_TRUST_MIN_REMAINING_PCT: float = 0.60

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

    # --- Liquidity threshold (lowered 50k -> 5k on 08-04, -> 1k on 08-06) --
    # Verified live 2026-08-06: Gamma's `liquidity` field is NOT reliable —
    # the API serves inconsistent cached slices, so the same live BTC 5-min
    # window reported ~$9k in one response and ~$500k in the next. A high
    # floor therefore makes eligible markets flicker in and out of discovery
    # depending on which cached slice a request hits. Real order-book depth
    # (the CLOB, which the strategy actually trades against) runs $500-$2k+
    # on live ETH/BTC windows. Paper mode caps per-trade size at 8% of $1000
    # = $80, so 1k depth is ample cover for realistic fills; the floor mainly
    # exists to drop the $1-liquidity dead windows.
    # NOTE: this is a PAPER-MODE calibration. Re-check before going live.

    # --- Paper trading ----------------------------------------------------
    STARTING_PAPER_BALANCE_USD: float = 1000.0

    # --- Telegram -----------------------------------------------------------
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_CHAT_ID: Optional[str] = None
    # How often the bot pushes a full status/stats digest to Telegram (hours).
    # In addition to the on-demand /status and /stats commands.
    TELEGRAM_STATUS_INTERVAL_HOURS: float = 6.0
    # Whether to run the command listener (polling) so you can query the bot
    # with /status, /stats, /help instead of only receiving push alerts.
    TELEGRAM_COMMANDS_ENABLED: bool = True
    # Start with routine (INFO/WARNING) alerts muted. CRITICAL alerts (kill
    # switch, settlement failures) always get through regardless — a mute must
    # never silence a safety message. Toggle at runtime with /mute and /unmute.
    TELEGRAM_MUTED_DEFAULT: bool = False

    # --- Event-driven fast path (win-the-gap) --------------------------------
    # The 1s polling cycle can miss a ~2s arbitrage window: a Binance move that
    # lands just after a cycle is only seen up to a second later, then two REST
    # book fetches and the simulated fill latency pile on top — worst case the
    # order lands at ~1.5-1.9s of a 2.0s window, which is why the bot looked
    # slow even though its internal latency was fine. The fast path reacts the
    # MOMENT a Binance WS tick shows a meaningful move, evaluating against the
    # live WS-cached Polymarket books (no REST round trip), so the true
    # tick->order gap drops to ~300-600ms. These two knobs control its
    # sensitivity: the cumulative price move (as a fraction) since the last
    # fast evaluation that triggers one, and the minimum gap between fast
    # evaluations per asset.
    FAST_PATH_MOVE_TRIGGER_PCT: float = 0.0010   # 0.10% move since last fast eval triggers one
    FAST_PATH_COOLDOWN_S: float = 0.5

    # --- Lag-gap measurement (instrumentation — never gates trading) -----------
    # Measures the bot's ACTUAL arbitrage window on this connection instead of
    # assuming it: LAG_TRACK_MOVE_MIN_PCT is the Binance move size that starts
    # a measurement, LAG_REPRICE_MIN_MOVE is how far the Polymarket token's
    # mid must move for the market to count as "repriced", and
    # LAG_TRACK_TIMEOUT_S is how long to wait before recording the market as a
    # laggard (timed_out). Purely diagnostic — no trade decision reads these.
    LAG_TRACK_MOVE_MIN_PCT: float = 0.0010   # 0.10% move triggers a lag measurement
    LAG_REPRICE_MIN_MOVE: float = 0.005      # absolute mid move (0.5c) counts as repriced
    LAG_TRACK_TIMEOUT_S: float = 30.0        # give the market this long to reprice
    LAG_TRACK_INTERVAL_S: float = 0.2        # how often the tracker loop scans

    # --- Latency / timing budget ---------------------------------------------
    # The realistic arbitrage window for Polymarket's short-duration crypto
    # up/down markets: measured quote-response lags cluster ~350ms with
    # exploitable dislocations up to ~2s (OpenMarket 2026 dataset; see
    # scripts/diagnose_timing.py for the full citation). This replaces the old
    # 2.7s guess in report_latency.py, which came from a secondhand article
    # rather than a measurement.
    ASSUMED_ARBITRAGE_WINDOW_S: float = 2.0
    # Polymarket's CLOB holds marketable orders on fast crypto up/down markets
    # for a 250ms taker-order-delay window (the itode market flag, per
    # docs.polymarket.com/concepts/order-lifecycle). This is ON TOP of our
    # measured tick->order latency, so any timing budget must add it explicitly.
    PLATFORM_TAKER_DELAY_MS: float = 250.0

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
