# Validation Run — August 2026

**Purpose:** one clean, unbiased, 7-day / 200+ trade measurement of the current
bot (event-driven fast path + entry gates). This is NOT a tuning session. The
values below are **frozen for the duration of this run — do not tune mid-run,
it invalidates the measurement.**

**Started:** 2026-08-07

---

## 1. Frozen configuration (snapshot from `config/settings.py`)

| Setting | Value |
|---|---|
| `FRESH_MOVE_MIN_PCT` | 0.0006 (0.06%) |
| `FRESH_MOVE_LOOKBACK_S` | 15.0 s |
| `MIN_ENTRY_TIME_REMAINING_S` | 45.0 s |
| `MAX_DIRECTIONAL_ENTRY_PRICE` | 0.80 |
| `TAKER_FEE_PCT` | 0.07 (crypto taker fee RATE — applied as rate·p·(1−p) per share, NOT flat) |
| `FAST_PATH_MOVE_TRIGGER_PCT` | 0.0010 (0.10%) |
| `FAST_PATH_COOLDOWN_S` | 0.5 s |

> These are frozen for the duration of this run — do not tune mid-run, it
> invalidates the measurement.

Also in effect (not part of the freeze list, recorded for completeness):
`EDGE_THRESHOLD_PCT=0.05` → **0.04 (2026-08-07, round-trip protocol)**,
`MIN_CONFIDENCE=0.85` → **0.75 (2026-08-07, round-trip protocol)**,
`MAX_POSITION_PCT=0.08`, `MAX_TOTAL_EXPOSURE_PCT=0.30`,
`DAILY_LOSS_HALT_PCT=0.20`, `TOTAL_DRAWDOWN_KILL_PCT=0.40`,
`SIMULATED_FILL_LATENCY_S=0.3`, `ASSUMED_ARBITRAGE_WINDOW_S=2.0`,
`PLATFORM_TAKER_DELAY_MS=250`.

> **2026-08-07 late: the round-trip protocol supersedes this run's framing.**
> The gate-frozen 7-day measurement is no longer the current objective — the
> strategy gained an exit-on-reprice protocol (see intervention log) that
> changes what's being measured. Re-run validation on the new protocol, not
> this frozen snapshot.

## 2. Calibration status

**UPDATED 2026-08-07 ~15:10 UTC (pre-measurement):** `config/calibration.json`
was fitted from 771,076 real 1-second BTC kline samples (10 days,
2026-07-28 → 2026-08-06, data.binance.vision) via
`scripts/calibrate_momentum_model.py` for the 5-min and 15-min horizons. This
happened BEFORE the clean measurement window below, so it does not
contaminate it.

**The fit's finding (important, honest):** momentum over the 30s lookback
predicts the 5-min outcome only ~52% of the time and the 15-min outcome
~51.1% — essentially a coin flip. The old hand-picked `sensitivity=8.0`
fallback was *inventing* ~98% probabilities from noise. The calibrated model
now reports honest ~0.51–0.52 probabilities, so the momentum fallback will
rarely (if ever) clear `EDGE_THRESHOLD_PCT` — the directional edge this
strategy can actually trade is the short-window lag arbitrage (fast path),
not momentum forecasting. This is the documented, expected state for this
run. (The fair-value model — the primary model when reference price +
volatility inputs are available — does not depend on calibration.json.)

## 3. Starting state

- **Fresh database.** The debug-run DB (7 trades, −$164.99, 4,300 signals —
  leftover from the 2026-08-06/07 diagnosis sessions) was archived to
  `storage/archive/2026-08-07_debug_run/`. The run starts from a fresh DB at
  the documented `STARTING_PAPER_BALANCE_USD = 1,000.00`.
- **Mode:** PAPER (`PAPER_MODE=True` in `.env`). `POLYGON_PRIVATE_KEY` unset —
  confirmed by preflight.
- **Commit:** `b81e3e5` (fast path + gates) plus the observability label for
  fast-path entries (`strategy = "latency_arb_fast"`).

## 4. Success criteria (from the run's acceptance gate)

Run `scripts/validate_paper_run.py` when the run completes:
- ≥ 200 completed paper trades
- ≥ 7 days elapsed since first trade
- win rate ≥ 70%
- positive expectancy per trade (net of fees & slippage)
- max drawdown < kill threshold (40%)
- plus `scripts/report_latency.py`, `scripts/check_calibration_drift.py`,
  and `scripts/analyze_run.py` (fast-path vs poll-path split + per-gate
  block counts).

## 5. Intervention log

A run with a documented human intervention is still usable; a run with an
undocumented one isn't trustworthy. Log **everything** done to the bot or its
processes here, with timestamps.

| Timestamp (UTC) | Action |
|---|---|
| 2026-08-07 12:45 | Clock correction (pre-run, before launch): preflight found the local clock +573 ms fast vs NTP. An attempt to step it back via `Set-Date` was sign-mangled by the shell and moved it forward to ~+4.9 s (measured). Corrected with `w32tm /resync /rediscover` (w32tm steps offsets ≥ 1 s since MaxAllowedPhaseOffset=1) -> offset now −5.5 ms. Verified < 50 ms. This happened BEFORE the run started, so it does not contaminate the measurement. |
| 2026-08-07 12:46 | Initial launch attempt via Task Scheduler (`scripts/launch_paper_run.py`, task `PolymarketArbBotPaper`). **Task Scheduler on this machine is broken**: every task reports "success" (Last Result 0) but nothing ever executes — proven with a trivial marker task and a .cmd/.ps1/powershell action. The task-launcher path was abandoned. |
| 2026-08-07 12:5x | Launch mechanism debugging (before the run): rewrote `scripts/start_paper_bot.cmd` (start /b + -u) and `scripts/launch_bot.ps1`; several short-lived bot launches from the sandbox (each killed after ~2 min by sandbox/process-group teardown, or by `taskkill` during cleanup). One such debug bot wrote 408 signal/latency rows to the then-fresh DB — that DB was archived to `storage/archive/2026-08-07_pre_run_debug/` and the run DB deleted so the validation run starts from a genuinely empty DB. |
| 2026-08-07 13:1x | Watchdog (step 5) built + verified end-to-end: it detected the dead bot and sent a real CRITICAL Telegram alert (twice during testing). Installed as a hidden loop in the Windows Startup folder (`watchdog_loop.vbs`, checks every 5 min, alerts after 15 min of silence, deduped to 1/hour). Task Scheduler unused because it is broken. Watchdog state file cleared after tests. |
| 2026-08-07 13:2x | **Run start (pending):** the bot is to be started by the user in a plain terminal window (double-click `run_paper_bot.cmd` and leave it open) — the mechanism the master prompt explicitly allows, since Task Scheduler is non-functional on this machine and the sandbox cannot hold persistent processes. The bot must NOT be launched from any coding-agent session. |
| 2026-08-07 ~14:00–15:00 | **Observation period (NOT part of the measurement):** the bot ran for ~2h with 0 trades. Diagnosis (prompted by user): (a) Polymarket WS reconnect flap (4 reconnects/10min) flipped the feed-health gate to UNHEALTHY, halting trading cycles by design; (b) even when healthy, every signal was gated — 836 `model read saturated` rows where the fair-value model read 0.02/0.98 against a market at 0.995/0.045, i.e. the model was confidently wrong in DIRECTION; (c) the market universe right now has windows expiring in seconds with asks at 0.9+ (entry-price cap correctly blocks). |
| 2026-08-07 ~15:10 | **Pre-measurement fixes (user-requested, before the clean window):** (1) fitted `config/calibration.json` (see §2) — replaces the noise-inventing fallback with honest fitted probabilities; (2) reference-price trust guard in `main.py`: discovery now only trusts a first-sighting Binance price as the fair-value reference when ≥ 60% of the window remains (`REFERENCE_TRUST_MIN_REMAINING_PCT=0.60`); a late first sighting leaves `reference_price` unset so fair-value stays off instead of emitting confidently-wrong saturated reads. Both verified by tests: **358 passing.** The frozen config values in §1 are UNCHANGED. |
| 2026-08-07 ~15:28 | **Sum-to-one bug found + fixed (pre-measurement):** a fresh-launch bot with the new calibration traded its first sum-to-one pair in 2 minutes and LOST $45.94. Root cause, two bugs in the "guaranteed win" path: (1) `_check_early_exits` applied directional TAKE_PROFIT/EDGE_REVERSAL exits to sum_to_one legs — the model "reversed" an ETH NO leg and sold it at 0.854 when holding to settlement would have paid 1.0, breaking the outcome-agnostic hedge; (2) `place_sum_to_one_order` validated the edge at decision time (best asks) but the fills land after 0.3s simulated latency + book walking — combined fill cost was 0.31+0.73=1.04 and 0.17+0.89=1.06 (a guaranteed LOSS), and nothing re-checked. Fixes: early exits now skip `strategy == "sum_to_one"`; the broker re-validates the locked edge from actual fills and reverses both legs (`SumToOneEdgeLostError`) if the combined cost is ≥ $1. The tainted DB (4 buggy trades, −$45.94) was archived to `storage/archive/2026-08-07_sum_to_one_bug/` and deleted — the measurement restarts from a genuinely clean $1,000. Tests: **361 passing.** |
| 2026-08-07 ~16:5x | **Instrumentation + feed-health change deployed (commit `077035b`, pre-measurement):** (1) fill-realism metrics now persisted (`slippage_pct`, `decision_best_ask`, `fill_best_ask` on `trades` + DB migration); (2) lag tracker (`engine/lag_tracker.py`, `lag_events` table) measures how long Polymarket takes to reprice after a Binance move — the empirical arbitrage window; (3) WS drops now log close code/reason; (4) **feed-health gate behavior change:** unhealthy now = staleness (>10s) OR an active 60s reconnect storm (≥4 in 60s), not the old "≥4 reconnects in 10 min". The old gate halted trading for bursts that had already recovered while books were 2ms fresh. The frozen config values are UNCHANGED. Tests: **372 passing.** |
| 2026-08-07 ~17:5x | **Review fixes deployed (pre-measurement):** (1) `watchdog_loop.vbs` now derives its repo path from `WScript.ScriptFullName` instead of a hardcoded absolute path — the watchdog can no longer silently break if the folder is moved/renamed/re-cloned; (2) sum-to-one legs now submit CONCURRENTLY (`asyncio.gather`) instead of sequentially — sequential submission let the book move for the full fill latency twice between the hedge's two halves (the root cause of the −$45.94 fills); plus a total-cost pre-check (kills the latent half-open-hedge risk) and reversal of a surviving leg if its sibling fails; (3) calibration fitter switched from `np.maximum.accumulate` to pool-adjacent-violators isotonic regression — **code only; `config/calibration.json` is NOT re-fitted**, the frozen run keeps the existing curve; (4) reference-price trust guard now FAILS CLOSED: unknown remaining time ⇒ reference not trusted (was: trusted); (5) feed health adds a sustained-reconnect hard limit (≥7 reconnects/10 min = unhealthy) on top of the storm gate; (6) watchdog docstring corrected (VBS loop, not Task Scheduler). Tests: **378 passing.** The frozen config values are UNCHANGED. |
| 2026-08-07 ~19:0x | **ROUND-TRIP PROTOCOL deployed (the hidden protocol from the AdiiX article).** The 98%-win-rate mechanism was never the entry — it's the EXIT: the article's bot "exits at this point for an immediate profit from the repricing" 2-3s after entering the lagging side. That is a bet that the market CORRECTS (near-certain), not a bet on the final outcome (a coin flip — our calibration: ~52%). We previously held every directional position to settlement, silently converting good lag entries into EV-negative outcome bets. Changes: (1) NEW `REPRICE` exit in `_check_early_exits` — exit when the held token has gained ≥ `REPRICE_EXIT_GAIN_PCT` within `REPRICE_EXIT_MAX_HOLD_S` (240s); book reads now prefer the zero-latency WS cache so exits are as fast as entries; (2) `EDGE_THRESHOLD_PCT` 0.05 → 0.04 (the article's "typically 3-5%" divergence band); (3) `MIN_CONFIDENCE` 0.85 → 0.75 (0.85 demanded ≥ $2100 book depth — nearly never true on live books, silently throttling the fast path to 0 trades/day). Sum-to-one legs remain exempt from REPRICE. Tests: **380 passing.** |
| 2026-08-07 ~20:3x | **REAL FEE MODEL deployed (verified against docs.polymarket.com/trading/fees — commit to be noted).** The flat 2% fee assumption was WRONG: Polymarket's crypto taker fee is price-dependent, `fee = rate · p · (1−p)` per share (rate = 0.07), i.e. ~3.5% of notional per side at p=0.50 (~7% for a taker round trip, before spread) and much smaller at high entry prices. The flat-2% model UNDERSTATED mid-price fees, so paper results looked better than live would be — and the round-trip threshold sat right at real break-even. Changes: (1) new `engine/fees.py` (`taker_fee_pct`, `round_trip_fee_pct`) used everywhere — paper broker entry/exit/sum-to-one fees, the signal engine's fee-aware gate, sum-to-one detection, backtest; (2) `TAKER_FEE_PCT` 0.02 → 0.07 as the RATE; (3) `REPRICE_EXIT_GAIN_PCT` 0.07 → 0.10 — with real fees the marginal 3-5% divergences are fee-food; the profitable round trips are the BIG reprices (the article's example is a 20+ point token move). The tweet bot's 98%-win-rate era predates Polymarket's March-2026 fee rollout (introduced to "curb high-frequency exploitation"). Tests: **386 passing.** |
| 2026-08-08 ~23:00 | **RUN RETIRED — see docs/VALIDATION_RUN_2026_08b.md for the replacement.** This run's data is not a clean frozen-config measurement: `EDGE_THRESHOLD_PCT`, `MIN_CONFIDENCE`, the round-trip exit protocol, and the fee model all changed while it was collecting (all above, post-`b81e3e5`), and the measurement instrument itself was broken for 20.7h (lag tracker recorded zero samples until 2026-08-08's fix). Live numbers at retirement: 0 directional trades fired out of ~16k signals since the Aug-07 restart (the momentum-fallback direction gate was mathematically impossible to pass — 3 consecutive same-direction ticks), the lag tracker had recorded exactly 1 measurement (ETH −0.14% Binance move, Polymarket repriced +0.64% after **21.4s** — one sample, not a verdict), and the Polymarket WS feed flapped repeatedly on the user's home network (~16 min unhealthy total in 9.3h, plus a current flap at retirement). DB archived to `storage/archive/2026-08-08_pre_v2_config/`. NOT a failure to log — a run with mid-run config changes isn't trustworthy, so it's being retired honestly. |
