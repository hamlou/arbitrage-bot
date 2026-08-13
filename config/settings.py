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
    # Discount applied to the half-Kelly fraction when the signal came from the
    # momentum FALLBACK model instead of the fair-value model. The fallback is
    # measured ~52%-accurate (calibration run 2026-08-08) — barely better than
    # a coin flip — so it must never be sized like a real fair-value read.
    # 0.5 = a fallback signal risks at most half of what an equal fair-value
    # lean would risk. Pure risk control: does not change which signals fire.
    MODEL_FALLBACK_SIZE_SCALE: float = 0.5
    # Weight of the Coinbase side in the composite (blended) price the model
    # reads: composite = BINANCE_WEIGHT * binance + (1 - BINANCE_WEIGHT) * coinbase.
    # The blend only activates while BOTH feeds are fresh and agree within
    # CROSS_EXCHANGE_TOLERANCE_PCT; otherwise the trustworthy side alone is
    # used, so a disagreeing/stale feed can never poison the signal.
    BINANCE_PRICE_WEIGHT: float = 0.5
    DAILY_LOSS_HALT_PCT: float = 0.20
    TOTAL_DRAWDOWN_KILL_PCT: float = 0.40
    # Total notional across ALL simultaneously open positions, as a fraction of
    # equity — separate from the per-trade MAX_POSITION_PCT cap. Without this,
    # a bot that's right about MAX_POSITION_PCT on every individual trade can
    # still stack enough concurrent positions to have far more than 8% of
    # equity at risk at once.
    MAX_TOTAL_EXPOSURE_PCT: float = 0.30

    # --- Signal thresholds --------------------------------------------------
    # Divergence band aligned with the latency-arb protocol (AdiiX article,
    # "The 4 strategies"): "When Polymarket's contract odds diverge from what
    # the CEX data implies by more than your threshold (typically 3-5%), buy
    # the correct side." Lowered 0.05 -> 0.04 on 2026-08-07 together with the
    # round-trip exit: entries are now short convergence bets (exit-on-reprice
    # banks the repricing, which usually overshoots the entry edge), so the
    # entry bar no longer needs to fund a hold-to-settlement outcome bet.
    # Net-of-fee threshold: raw edge must clear the price-dependent round-trip
    # fee (engine/fees.py) on top — ~7% of notional at p=0.5, less at higher
    # entry prices.
    EDGE_THRESHOLD_PCT: float = 0.04
    # Confidence is mean(freshness, depth/2000, consistency) — the consistency
    # term is always 1 on the fair-value path, so this gate's real job is to
    # reject STALE data (freshness decays to 0 by 5s) and near-empty books.
    # 0.85 required depth >= $2100 — nearly never true on live BTC/ETH books
    # ($500-2000), which silently throttled the fast path to ~0 trades/day
    # regardless of how many real divergences appeared. 0.75 keeps the
    # stale-data protection while letting depth >= ~$600 books trade.
    # Lowered 2026-08-07.
    # Lowered 0.75 -> 0.70 on 2026-08-13 (measured, not guessed): live book
    # depth on the target ask side runs median ~$292 (p75 ~$719) across all
    # evaluations, and the would-fire signals (net edge > 4% after fees,
    # entry <= 0.45) have median depth ~$488 with only 49% clearing the
    # $500-equivalent 0.75 bar but 84.5% clearing 0.70 (depth >= ~$200,
    # which still rejects genuinely empty books). 0.70 keeps the stale-data
    # protection (freshness still decays to 0 by 5s) while no longer
    # demanding more depth than the books these signals actually trade on
    # offer.
    MIN_CONFIDENCE: float = 0.70
    MIN_MARKET_LIQUIDITY_USD: float = 1_000.0

    # --- Entry discipline (directional strategy) ---------------------------
    # Never buy a directional token above this price. Buying at 0.82 means
    # being right 82% of the time just to break even before fees — a
    # miscalibrated model read on a 5-minute window deserves no such
    # assumption. Verified 2026-08-06: the bot opened YES @ 0.82 and NO @ 0.99
    # (the latter can mathematically never profit after fees) and lost ~$170
    # on two of those entries. 0.80 is deliberately conservative.
    # Lowered 0.80 -> 0.70 on 2026-08-09: measured on 79 distinct blocked
    # markets, winners entered at avg 0.39 while the 12 losers entered at avg
    # 0.57 — the expensive entries are where the losses concentrate. A 0.70
    # cap blocks 25% of the losers for the cost of only 1% of the winners.
    # Lowered 0.70 -> 0.58 on 2026-08-11 (PROVISIONAL — see the freeze rule in
    # docs/VALIDATION_RUN_2026_08b.md): live fills agree with the 79-market
    # study — all 3 live entries at >= 0.60 lost (0.60/0.63/0.66 -> -$131 net)
    # while 0 of 12 winners entered above 0.52. Expensive entries carry
    # open-ended downside (a 0.63 NO collapsed to 0.12) against capped 10%
    # upside, and the fee savings at high prices are real but small. 0.58 sits
    # just under the historical loser average (0.57) with margin; the signal
    # gate logs every blocked entry (reason=entry ask > max), so the forward
    # record will judge this — re-evaluate at 100+ trades, do NOT tune before.
    # Lowered 0.58 -> 0.45 on 2026-08-13 (FREEZE OVERRIDE — user-directed,
    # logged in docs/VALIDATION_RUN_2026_08b.md): the forward record since
    # the 0.58 cap landed is 6 trades, and ALL FOUR entries at >= 0.50 lost
    # (0.50/0.50/0.52/0.54 -> -$172.5 net, 0 wins) while the 0.27 entries
    # went 1W/1L. That matches the mechanism, not luck: at p~0.5 the contract
    # sits at maximum uncertainty with minimum relative mispricing (nothing
    # to be right about that isn't a coin flip) and the fee is at its dollar
    # peak — the model has its weakest edge exactly where losses are worst,
    # and ZERO wins have ever come from an entry >= 0.50 in this project's
    # entire record. 0.45 clears the 0.50 max-fee zone with margin while
    # keeping the historical winner zone (avg entry 0.39) fully tradable.
    MAX_DIRECTIONAL_ENTRY_PRICE: float = 0.45
    # Polymarket's crypto taker fee RATE — NOT a flat fraction. The per-share
    # fee is fee_rate * p * (1 - p); as a fraction of what you spend that is
    # fee_rate * (1 - p) per side (~3.5% of notional at p=0.50, ~1.5% at
    # p=0.80). A taker round trip pays it twice, so at p~0.5 the fee hurdle
    # is ~7% of notional before spread. All fee math lives in engine/fees.py;
    # this setting is the single source of truth for the RATE (directional
    # universe fallback).
    # CONFIRMED 2026-08-12 against docs.polymarket.com/trading/fees (the
    # authoritative page now loads): "Crypto: taker fee rate 0.07" — the
    # earlier 0.06 was a best-effort estimate from 2026-08-08 while the page
    # was unreachable. The fee is CATEGORY-dependent: crypto 0.07, sports/
    # economics/culture/weather/other 0.05, finance/politics/mentions/tech
    # 0.04, geopolitics FREE (fee_rate_for_category in engine/fees.py is the
    # single source of truth; the sum-to-one scan applies it per market). The
    # flat 2% assumption (pre-2026-08-07) UNDERSTATED mid-price fees — paper
    # results looked better than live.
    TAKER_FEE_PCT: float = 0.07
    # Cross-exchange sanity gate: before firing a signal, the latest known
    # Binance and Coinbase prices for the asset must agree within this many
    # percent (0.5 = 0.5%). A bigger divergence usually means one feed is
    # stale or misbehaving — don't trade on it. The check is skipped (signal
    # allowed) when either source hasn't delivered a tick yet, since the bot
    # has always been able to run on Binance alone.
    # Raised 0.1 -> 0.5 on 2026-08-13 (measured, not guessed): live audit data
    # from the running bot showed Binance vs Coinbase disagreement on BTC/ETH
    # sits at 0.10–0.133% essentially ALWAYS (2,000 consecutive observations,
    # min 0.100, max 0.133) — i.e. the old 0.1% tolerance was below the
    # exchanges' own normal quote-level noise, so the gate tripped on 100% of
    # evaluations and silently killed 62% of ALL signals (1,237 of 2,000). The
    # gate exists to catch a genuinely misbehaving feed (which shows as 1%+
    # divergence or staleness, both still caught at 0.5%), not normal
    # two-venue microstructure noise. Re-simulated against the same 2,000
    # signals: 0.5% tolerance turns 1 fired signal into ~355 would-fire
    # opportunities (net-of-fee edge > 4%, entry <= 0.45 cap).
    CROSS_EXCHANGE_TOLERANCE_PCT: float = 0.5

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
    # Large-edge bypass of the fresh-move magnitude floor (verified 2026-08-09
    # on 41k logged evaluations): the fresh-move gate exists to reject STALE
    # DRIFT passed off as a fresh lag (-$35 loss). But measuring shows it also
    # blocks the strategy's best trades: 4,033 fair-value signals with edge
    # > 5pt were blocked, and 90% of those markets converged toward the model's
    # read (95% simulated win rate at realistic costs; median reprice < 60s).
    # When the model-vs-market edge is THIS large (>= this threshold), the
    # divergence itself is the signal — the market is lagging something real,
    # even if the last 15s happened to be flat. The bypass still REQUIRES the
    # recent move to agree in DIRECTION (no fresh move in the model's
    # direction = nothing to lag = still blocked); it only drops the magnitude
    # floor. Small edges keep the strict gate.
    FRESH_MOVE_LARGE_EDGE_BYPASS_PCT: float = 0.12
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
    # Whether the momentum heuristic/calibration fallback may fire ENTRY
    # signals when the fair-value model has no inputs (no reference price or
    # volatility estimate). Verified live 2026-08-11: this fallback is a coin
    # flip — it fires with ZERO knowledge of the market's reference price, and
    # ALL THREE full-stake SETTLED-at-zero losses (−$67, −$85, −$77 = −$229)
    # came from momentum_fallback entries (3W/3L, −$173 net) while fair-value
    # trades were 7W/2L (+$33 net). The bot is a gap founder, not a gambler:
    # no reference -> no entry. Keep False unless out-of-sample data shows the
    # fallback is profitable.
    ALLOW_MOMENTUM_FALLBACK_ENTRIES: bool = False

    # --- Exit logic -----------------------------------------------------
    # Early take-profit: exit if the position's current mark-to-market value
    # has gained at least this fraction of stake before contract expiry.
    TAKE_PROFIT_PCT: float = 0.5
    # Exit early if the live edge (recomputed each cycle) has flipped against
    # the position's original side by more than this — i.e. our own model no
    # longer agrees with the trade we're holding.
    EDGE_REVERSAL_EXIT_THRESHOLD_PCT: float = 0.10
    # Do not let EDGE_REVERSAL fire within this many seconds of entry. The
    # reprice the strategy banks on takes time to develop (live 2026-08-10:
    # median reprice win hold = 30s), and the model is least reliable in the
    # seconds right after it just told us to buy — live reversals fired 1-45s
    # after entry, several of them selling positions that then repriced to a
    # win (winners dip BELOW entry before converging — the same mechanism
    # that makes stop-losses destroy this strategy). A reversal before the
    # market has had a chance to converge is the model contradicting its own
    # fresh entry, not a real edge flip.
    EDGE_REVERSAL_MIN_HOLD_S: float = 60.0

    # --- Round-trip protocol (exit on reprice — the 98%-win-rate piece) ----
    # The strategy's high win rate does NOT come from prediction. It comes
    # from EXITING ON CONVERGENCE: enter the lagging side, then sell when
    # Polymarket reprices toward the true value (AdiiX article: "Within 2-3
    # seconds ... the odds shift from 54/46 toward the 'true' 78/22. The bot
    # can exit at this point for an immediate profit from the repricing").
    # That is a bet that the market CORRECTS (near-certain), not a bet on
    # the final outcome (a coin flip — our own calibration measured ~52%).
    # Holding every position to settlement was silently converting each good
    # lag entry into an EV-negative outcome bet (EV = p*win - (1-p)*stake
    # with p~0.52 and a 2% fee each way). Two knobs:
    #   REPRICE_EXIT_GAIN_PCT: exit when the held token has gained this
    #     fraction from entry. Break-even is the WHOLE round trip: the real
    #     price-dependent taker fee (~3.5% of notional per side at p=0.5, so
    #     ~7% round trip) PLUS the bid-ask spread we cross twice (~1-2c on a
    #     ~0.50 book = ~2-4%). A ~10% token gain therefore nets roughly +2-3%
    #     of size at mid prices after fees+spread, and the BIG reprices — the
    #     article's example is a 20+ point token move (a 0.6% BTC shift moves
    #     a 15-min window's probability by 20+ points) — bank 10-25%+. The
    #     marginal 3-5% divergences are fee-food, exactly why Polymarket's
    #     fee rollout (March 2026) compressed the strategy for small takers.
    #     Deliberately checked BEFORE TAKE_PROFIT: a position that would
    #     settle +60% exits at the reprice threshold instead — the round-trip
    #     caps winners on purpose, trading cadence and win rate for the
    #     occasional big hold.
    #   REPRICE_EXIT_MAX_HOLD_S: if the market hasn't repriced within this
    #     window, the arbitrage is gone — stop waiting and let the normal
    #     exits (TAKE_PROFIT / EDGE_REVERSAL / settlement) take over.
    REPRICE_EXIT_GAIN_PCT: float = 0.10
    REPRICE_EXIT_MAX_HOLD_S: float = 240.0
    # Fee-aware exit floor (added 2026-08-11): the entry gate already nets
    # the price-dependent round-trip fee (round_trip_fee_pct) before allowing
    # a trade; the exit must too, or a "REPRICE win" can be a loss after fees
    # on cheap entries where the flat REPRICE_EXIT_GAIN_PCT sits below
    # break-even (p < ~0.20). The actual exit threshold is
    #   max(REPRICE_EXIT_GAIN_PCT, round_trip_fee_pct(entry, exit) / entry
    #       + REPRICE_EXIT_FEE_MARGIN)
    # At mid prices the flat 10% already clears the fee, so this only binds
    # on cheap entries.
    REPRICE_EXIT_FEE_MARGIN: float = 0.01

    # --- Sum-to-one (combo) arbitrage -----------------------------------------
    # If YES_ask + NO_ask (net of modeled fees) is under $1 by at least this
    # much, buying both sides locks in a risk-free profit at settlement
    # regardless of outcome — this doesn't depend on directional forecasting
    # at all, so it gets its own threshold and its own position cap rather
    # than sharing Kelly sizing with the directional strategy.
    SUM_TO_ONE_MIN_EDGE_PCT: float = 0.01
    SUM_TO_ONE_MAX_POSITION_PCT: float = 0.10

    # --- Cross-window (5m/15m same-endTime) arbitrage --------------------------
    # Second risk-free leg (added 2026-08-13, sourced from public bot
    # research — verified mathematically, not copied): two Polymarket windows
    # on the SAME asset with the SAME endTime resolve against the SAME
    # end-of-window BTC/ETH price but have DIFFERENT beat prices (the price
    # at each window's own open). If the lower-beat window's beat is X and
    # the higher-beat window's is Y, then buying UP on the lower-beat window
    # + DOWN on the higher-beat window pays >= $1 for every possible outcome
    # (exactly $1 in the two outer bands, $2 in the middle band where
    # X < final price <= Y) — the same arithmetic guarantee as sum-to-one,
    # exploited across windows instead of across YES/NO tokens. Own position
    # cap and min edge, mirroring the sum-to-one settings.
    CROSS_WINDOW_ENABLED: bool = True
    CROSS_WINDOW_MIN_EDGE_PCT: float = 0.01
    CROSS_WINDOW_MAX_POSITION_PCT: float = 0.10
    # Minimum relative gap between the two windows' beat prices (fraction of
    # the lower beat). Guarantees (a) the pair is genuinely two different
    # windows (a zero gap degenerates into sum-to-one) and (b) the beat
    # ORDERING is robust to the reference-price approximation error — the
    # wrong order turns the guaranteed arb into a directional bet, so the
    # gap must be large relative to the capture noise.
    CROSS_WINDOW_MIN_BEAT_GAP_PCT: float = 0.0005   # 0.05% (~$30 at $60k)
    # The reference price is captured at first sighting; it approximates the
    # window's real beat only when captured within a few seconds of the
    # window's OPEN. Discovery runs every ~3s, so a timely sighting is a few
    # seconds late; this is the maximum allowed capture delay before the
    # reference is considered too stale to trust for beat ordering.
    CROSS_WINDOW_MAX_REF_CAPTURE_DELAY_S: float = 10.0
    # --- Sum-to-one universe (Lever 1, added 2026-08-08) ------------------
    # The sum-to-one check is arithmetic and works on ANY binary market, but
    # discovery was confined to crypto up/down (tag_slug=up-or-down) — the
    # single most fee-heavy, most latency-bot-crowded corner of the platform.
    # This flag widens the risk-free scan to all binary categories (politics,
    # sports, geopolitics, long-duration crypto...), where several categories
    # are cheaper or fee-free. The DIRECTIONAL universe stays BTC/ETH-only;
    # only the sum-to-one scan consumes this list.
    SUM_TO_ONE_DISCOVERY_ENABLED: bool = True
    SUM_TO_ONE_DISCOVERY_MIN_LIQUIDITY_USD: float = 500.0
    SUM_TO_ONE_DISCOVERY_LOOKAHEAD_S: float = 24 * 3600  # markets ending within 24h
    SUM_TO_ONE_DISCOVERY_MAX_MARKETS: int = 60           # cap book-polling load
    SUM_TO_ONE_DISCOVERY_MAX_PAGES: int = 5
    # --- Gap-timed exit (added 2026-08-12, measurement-first) ------------
    # If a position hasn't repriced within this multiple of the asset's
    # MEASURED median REPRICE hold (from closed REPRICE trades), the
    # convergence the strategy banks on has likely failed — exit instead of
    # waiting out the flat REPRICE_EXIT_MAX_HOLD_S (240s) backstop exposed to
    # a reversal. This is the "enter and get out inside the gap" idea applied
    # to the EXIT side — no new direction prediction. Measurement-first: with
    # zero closed REPRICE trades for an asset the exit stays OFF (no data
    # yet), and GAP_EXIT_MIN_HOLD_S floors the trigger so a thin sample can
    # never force a sub-minute exit. Provisional — re-evaluate at the 100+
    # trade bar like any other threshold.
    GAP_EXIT_MULTIPLIER: float = 1.5
    GAP_EXIT_MIN_HOLD_S: float = 30.0
    # No-progress exit (added 2026-08-13): if a position has NEVER once
    # traded above entry (MFE < 0 — the walked executable bid never crossed
    # entry) after NO_PROGRESS_HOLD_S, the model's predicted convergence
    # never materialized at all — exit instead of riding to settlement at 0.
    # GAP_EXPIRED covers the "didn't reprice in time" case but only once an
    # asset has closed REPRICE winners to measure a median; this is the
    # stats-free backstop for the never-green loser class (live 2026-08-13: a
    # BTC NO @ 0.27 that never went positive was held 540s to settlement at 0
    # -> -$36.69; three more never-green trades lost -$90). REPRICE winners
    # close in ~27s median (the live win repriced within 26.6s), so a 120s
    # (4.4x median) never-green hold is a dead trade, not a slow one — and
    # the MFE<0 guard means a trade that ever crossed entry is never touched.
    NO_PROGRESS_HOLD_S: float = 120.0

    # --- Sum-to-one maker execution (added 2026-08-12) ------------------
    # Takers pay the price-dependent fee (rate * p * (1 - p) per share);
    # MAKERS pay ZERO and earn a rebate (20% on crypto). Instead of taking
    # BOTH legs, post ONE leg as a resting maker order at the bid and, the
    # instant it fills, take the other leg at market — roughly half the fee
    # AND a strictly cheaper maker price (bid < ask). The exposure window
    # between the two legs is one API round trip, not an open-ended
    # cancel-and-reverse wait, so a naked directional position can never
    # persist. The cheap leg is the maker (its fee is the larger fraction of
    # notional), the expensive leg is taken. The resting order is
    # depth-checked (never size beyond what the counterparty book can match)
    # and cancelled if the lock dies before it fills.
    SUM_TO_ONE_MAKER_ENABLED: bool = True
    SUM_TO_ONE_MAKER_TIMEOUT_S: float = 90.0

    # --- Market discovery -----------------------------------------------
    # Decoupled from the (much faster) per-cycle signal evaluation loop — no
    # reason to re-hit Gamma every second just to catch new 5/15-min markets.
    MARKET_DISCOVERY_INTERVAL_S: float = 3.0
    # Discovery-dry tolerance: Gamma intermittently serves stale cached slices
    # that contain NO live windows at all (verified live 2026-08-11 — 0 live
    # BTC/ETH windows across 40 paginated pages while the API status page said
    # "operational"; re-verified 2026-08-12 — the SAME query returned 0 events
    # one minute and 100 events the next). When a pass returns zero markets we
    # must NOT wipe the known universe (that empties the WS subscription,
    # which flips the feed-health gate to unhealthy, which halts trading) —
    # see main.py. After this many CONSECUTIVE empty discovery passes, alert
    # Telegram instead of silently idling.
    #
    # Raised 3 -> 60 on 2026-08-12: at 3 passes (x 3s = ~9s) the CRITICAL
    # alert fired on every transient Gamma blip — alert fatigue that read as
    # "the bot is broken" when discovery was actually recovering a second
    # later (live-verified: the bot's own discovery found 4 live BTC/ETH
    # 5-min windows while the alert was still firing). 60 passes x 3s = 3
    # minutes of SUSTAINED emptiness before CRITICAL — long enough to be a
    # real outage, short enough that the 5-min windows (which expire in a
    # couple of minutes with no replacements) haven't silently emptied the
    # universe. The bot keeps trading off last-known markets meanwhile.
    DISCOVERY_EMPTY_ALERT_AFTER_PASSES: int = 60

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
    # How often the exit-forensics digest (premature-vs-protective EDGE_REVERSAL
    # split + freeze/live-gate progress) is pushed to Telegram. Pure reporting —
    # closes the loop on the freeze rule without touching any threshold.
    TELEGRAM_FORENSICS_INTERVAL_HOURS: float = 24.0
    # Whether to run the command listener (polling) so you can query the bot
    # with /status, /stats, /help instead of only receiving push alerts.
    TELEGRAM_COMMANDS_ENABLED: bool = True
    # Start with routine (INFO/WARNING) alerts muted. CRITICAL alerts (kill
    # switch, settlement failures) always get through regardless — a mute must
    # never silence a safety message. Toggle at runtime with /mute and /unmute.
    TELEGRAM_MUTED_DEFAULT: bool = False

    # --- Cloud persistence (ephemeral hosts: Render free wipes local disk) -----
    # When enabled, a consistent snapshot of the SQLite ledger is sent to
    # TELEGRAM_CHAT_ID every CLOUD_BACKUP_INTERVAL_MIN minutes, and on boot
    # with a missing DB the bot asks the user to forward the latest backup to
    # restore it. Fully inert (no network calls) when disabled, so local runs
    # are completely unaffected.
    CLOUD_BACKUP_ENABLED: bool = False
    CLOUD_BACKUP_INTERVAL_MIN: float = 15.0
    CLOUD_RESTORE_TIMEOUT_S: float = 120.0

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
    # Lowered 2026-08-08 from 0.10% to 0.03%: with the per-tick bug fixed, a
    # 0.10% CUMULATIVE trigger still only fired ONE measurement in 9h of
    # runtime (quiet BTC/ETH sessions rarely move 0.10% within a few seconds).
    # 0.03% captures 3-10x more samples — more diagnostic data, and it NEVER
    # gates trading (this block is instrumentation only). max_pending=200 in
    # LagTracker still fits comfortably: pending measurements resolve within
    # LAG_TRACK_TIMEOUT_S (30s), so even at the higher trigger rate only a
    # handful are in flight per symbol.
    LAG_TRACK_MOVE_MIN_PCT: float = 0.0003   # 0.03% CUMULATIVE move (from baseline) triggers a lag measurement
    LAG_REPRICE_MIN_MOVE: float = 0.005      # absolute mid move (0.5c) counts as repriced
    LAG_TRACK_TIMEOUT_S: float = 30.0        # give the market this long to reprice
    LAG_TRACK_INTERVAL_S: float = 0.2        # how often the tracker loop scans
    # If a per-symbol lag baseline hasn't triggered a measurement within this
    # many seconds, re-anchor it to the current price (added 2026-08-08: the
    # tracker compared per-tick moves and recorded ZERO measurements in 20.7h
    # — trade-by-trade data never clears a 0.10% single-tick move). A move
    # that never triggered in time isn't a front-runnable lag anymore; the
    # market has likely already priced it.
    LAG_TRACK_BASELINE_MAX_AGE_S: float = 5.0

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
        "CROSS_WINDOW_MAX_POSITION_PCT",
        "EDGE_REVERSAL_EXIT_THRESHOLD_PCT",
        "REPRICE_EXIT_GAIN_PCT",
        "REPRICE_EXIT_FEE_MARGIN",
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
