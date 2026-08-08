# Validation Run — August 2026 (v2: round-trip protocol era)

**Purpose:** one clean, unbiased, 7-day / 200+ trade measurement of the bot AS
IT IS NOW. This is NOT a tuning session. Every value below is **frozen for the
duration of this run — do not tune mid-run, it invalidates the measurement.**

**Started:** 2026-08-08 (after the Aug-07 run was retired — see
`docs/VALIDATION_RUN_2026_08.md` for why: EDGE_THRESHOLD_PCT, MIN_CONFIDENCE,
the round-trip exit protocol, and the fee model all changed mid-run).

**Commit the run started at:** `4c5deb4`
("Add single-instance guard, correct fee rate to 0.06, and widen lag
measurement"). If a future question asks "why does this differ", this is the
fixed reference point.

**What this run measures:** the **round-trip exit protocol** is now part of the
strategy — enter a lagging side on a fresh Binance move, exit on the repricing
(`REPRICE` exit) within seconds-to-minutes instead of holding to settlement.
This is a meaningfully different bot than the original hold-to-settlement
attempt, and that is fine — it is the exit protocol from the article that
produced the 98%-win-rate numbers (an exit bet on the market CORRECTING, not a
coin-flip bet on the final outcome).

---

## 1. Frozen configuration (complete list — every setting that affects trade
selectivity or sizing)

| Setting | Value | Notes |
|---|---|---|
| `EDGE_THRESHOLD_PCT` | 0.04 | entry edge gate (4%) |
| `MIN_CONFIDENCE` | 0.75 | freshness/depth/consistency gate |
| `FRESH_MOVE_MIN_PCT` | 0.0006 (0.06%) | aligned 15s move required to fire |
| `FRESH_MOVE_LOOKBACK_S` | 15.0 s | |
| `MIN_ENTRY_TIME_REMAINING_S` | 45.0 s | no entries in the window's final stretch |
| `MAX_DIRECTIONAL_ENTRY_PRICE` | 0.80 | never buy a directional token above this |
| `TAKER_FEE_PCT` | 0.06 | **the RATE**, not a flat fee — applied as `rate · p · (1−p)` per share (see §2) |
| `FAST_PATH_MOVE_TRIGGER_PCT` | 0.0010 (0.10%) | fast-path trigger |
| `FAST_PATH_COOLDOWN_S` | 0.5 s | |
| `REPRICE_EXIT_GAIN_PCT` | 0.10 | exit when the held token has gained this |
| `REPRICE_EXIT_MAX_HOLD_S` | 240.0 s | give up on the reprice after this |
| `MAX_POSITION_PCT` | 0.08 | per-trade cap (half-Kelly, 8%) |
| `MAX_TOTAL_EXPOSURE_PCT` | 0.30 | across all open positions |
| `DAILY_LOSS_HALT_PCT` | 0.20 | daily halt at −20% |
| `TOTAL_DRAWDOWN_KILL_PCT` | 0.40 | kill switch at −40% |

> These are frozen for the duration of this run — do not tune mid-run, it
> invalidates the measurement.

Not part of the trade freeze (measurement instrumentation only, does not gate
trading): `LAG_TRACK_MOVE_MIN_PCT` = 0.0003 (0.03%) — lowered from 0.10% on
2026-08-08 so the lag instrument records far more samples; more diagnostic
data is unambiguously good and changes no trade decision.

## 2. Fee model (verified)

`TAKER_FEE_PCT` is a RATE, not a flat fee. Verified 2026-08-08 against
https://docs.polymarket.us/fees ("Fee Schedule - Polymarket US Documentation",
effective 12 AM ET Wednesday July 1, 2026): "Fees are computed using a
symmetric formula that scales with price uncertainty: **Fee = Theta × C × p ×
(1 - p)**", with Theta (taker fee coefficient) = **0.06** and maker rebate
−0.0125. As a fraction of notional spent that is `rate · (1−p)` per side —
~3% at p=0.50, ~1.2% at p=0.80 — and a taker round trip pays it twice
(~6% of notional at p~0.5 before spread). The prior 0.07 value overstated the
coefficient by ~17% (the safe direction, but it throttled marginal trades).

## 3. Calibration status

`config/calibration.json` exists (fitted 2026-08-07 from 771,076 real BTC
1-second kline samples, 5-min and 15-min horizons). The fit's honest finding:
30s momentum predicts a 5-min outcome ~52% of the time — essentially a coin
flip. The calibrated momentum model therefore rarely fires; the strategy's
tradable edge is the short-window lag arbitrage (fast path + REPRICE exit),
which does not depend on momentum prediction. **Known limitation:** calibration
was fitted before this run and is part of the frozen state — do not re-fit
mid-run.

## 4. Starting state

- **Fresh-ish DB:** the previous run's DB (0 directional trades, 1 lag
  measurement, −$82.80 from 6 pre-fix sum-to-one trades) was archived to
  `storage/archive/2026-08-08_pre_v2_config/`. The run continues in the same
  `storage/arb_bot.db` (paper balance carried over: ~$917).
- **Mode:** PAPER (`PAPER_MODE=True` in `.env`). `POLYGON_PRIVATE_KEY` unset.
- **Single-instance guard:** `storage/bot.lock` (added `4c5deb4`) — a second
  bot process refuses to start; verified by test.
- **Known environmental risk:** the Polymarket WS feed flaps on the user's
  home WiFi (~16 min unhealthy in 9.3h on the previous run). Ethernet is the
  recommended fix. Feed flaps are logged in the intervention log below, not
  silently ignored.

## 5. Success criteria (from the run's acceptance gate)

Run `scripts/validate_paper_run.py` when the run completes:
- ≥ 200 completed paper trades
- ≥ 7 days elapsed since first trade
- win rate ≥ 70%
- positive expectancy per trade (net of fees & slippage)
- max drawdown < kill threshold (40%)
- plus `scripts/report_latency.py`, `scripts/check_calibration_drift.py`,
  and `scripts/analyze_run.py` (fast-path vs poll-path split + per-gate
  block counts).

**Honesty clause:** if the real fees (see §2) mean the round-trip exit
strategy is barely profitable or unprofitable after costs, that is the answer
this run exists to produce. Report the honest numbers, not the hoped-for ones.

## 6. Intervention log

A run with a documented human intervention is still usable; a run with an
undocumented one isn't trustworthy. Log **everything** done to the bot or its
processes here, with timestamps.

| Timestamp (UTC) | Action |
|---|---|
| 2026-08-08 ~23:00 | Run started (commit `4c5deb4`). Previous run retired and archived; fee rate corrected to 0.06; lag trigger widened to 0.03%; single-instance guard enabled. |

---

*Continue logging every intervention below — a run with a documented human
intervention is still usable; a run with an undocumented one isn't
trustworthy.*
