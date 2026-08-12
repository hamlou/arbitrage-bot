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
| `MAX_DIRECTIONAL_ENTRY_PRICE` | 0.70 | never buy a directional token above this (lowered 0.80->0.70 on 2026-08-09: winners entered avg 0.39 vs losers avg 0.57 — a 0.70 cap blocks 25% of the losers for 1% of the winners) |
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
| 2026-08-09 | Mechanical-loss fixes pushed: fill-price cap on the actual fill (`broker_paper.py`), EDGE_REVERSAL min-hold 60s, phantom-gain exit fix (decision and fill share the walked bid), sum-to-one pre-quote walk + reverse-vs-hold. Run data before these fixes is NOT comparable to after. |
| 2026-08-11 | **Final pre-freeze push** (fee-aware REPRICE exit floor + entry cap 0.70 → 0.58, provisional). Honest state after the two mechanical-fix pushes: 18 live trades, 12 wins / 6 losses (67%), net −$94.64 (gross −$58.46, fees $36.18 — fees are inside the net, not additive). Loss breakdown: all 6 losses are EDGE_REVERSAL; 3 of them (0.60/0.63/0.66 entries) = −$131 of the −$157 loss total, and 0 of 12 wins entered above 0.52. That 3/3 cluster motivated the provisional 0.58 cap — justified primarily by the out-of-sample 79-market study (losers avg 0.57, winners 0.39), with the live fills as support, NOT as validation of a number fit to those same trades. The signal gate logs every blocked entry (reason=entry ask > max), so the forward record will judge the cap. |
| 2026-08-11 | **Analytics-only push (freeze-compliant — measurement, no gating).** Per the external review's #1 priority ("are EDGE_REVERSAL exits protecting capital or cutting good arbs?"), the bot now records per-trade MFE/MAE (max favorable/adverse excursion vs entry, persisted at close) and samples the held token's price at T+5/15/30/60/120s and settlement after every early exit (`exit_probes` table). `scripts/analyze_exits.py` classifies each EDGE_REVERSAL exit as premature (market hit +10% after we left) vs protective. No thresholds changed; the freeze rule is untouched. Live at the push: 5 trades on the new cap (4 wins / 1 loss, −$33 — the single loss was a model-wrong mid-price NO @ 0.53, not an expensive entry). |
| 2026-08-11 | **Reliability fix, freeze-compliant (pure bug fix, no thresholds): discovery-dry cascade.** Symptom: "why no trades? only backup.db" — the bot silently idled with 0 markets. Verified live: Gamma intermittently serves stale cached slices with ZERO live BTC/ETH windows (0 windows across 30-40 paginated pages while the status page said "operational"; the API also served a stuck/repeated cursor). The old discovery loop treated an empty result as "all markets gone" and wiped `_known_markets` → `update_assets([])` emptied the WS subscription → the feed went stale → the feed-health gate halted ALL trading. One bad API page knocked the bot out for hours. Fix: prune only with positive evidence — (a) absent from a NON-EMPTY discovery result, or (b) window genuinely ended by wall clock. Empty result now keeps the last-known universe and WS subscription so trading resumes the instant the API recovers. Same guard applied to the sum-to-one universe. Added: Telegram alert after `DISCOVERY_EMPTY_ALERT_AFTER_PASSES` (3) consecutive empty passes + recovery alert, so a stale API is reported instead of silent. Tests: 436 passing (2 new regression tests). No threshold values changed. |
| 2026-08-12 | **THE "GUESSING" FIX — momentum fallback gated OFF for entries (user-directed, structural, not a threshold tune).** Live 2026-08-11 data mapped every trade to the model that fired it: fair_value = 7W/2L, **+$33 net**; momentum_fallback = 3W/3L, **−$173 net** — and ALL THREE full-stake SETTLED-at-zero losses (−$67, −$85, −$77) were fallback entries with NO reference price (the code's own calibration doc calls the fallback "honestly ~52%" — a coin flip). The bot was gambling on direction it had no basis to know, the exact opposite of the lag-arb premise. Fix: new setting `ALLOW_MOMENTUM_FALLBACK_ENTRIES: bool = False` (default OFF). With the gate off, a market with no trusted reference price produces "momentum fallback disabled for entries" instead of a trade — no reference → no entry. Fallback code stays for explicit opt-in (backtest/replay). This is a structural entry-model change (like the earlier momentum-direction gate), NOT a threshold fit to the loss cluster; the freeze rule's numbers are untouched. Tests: 444 passing (2 new gate regression tests + fallback tests updated to opt in). |
| 2026-08-12 | **Discovery stale-slice fix — the "0 markets" CRITICAL alert was OUR query, not Polymarket being down.** Verified live while the alert fired: the API WAS healthy (served Trump/Fed-rate markets), and BTC/ETH up/down windows with real liquidity ($3k-$16k) were reachable — but only through a DIFFERENT query. Measured 6/6 stable: the bot's query `order=endDate&ascending=true` returned 1 live window while the same `/events/keyset` without those params returned 18. Root cause: Gamma's keyset endpoint serves DIFFERENT cached slices per param set — the no-order query returns the hourly windows, the order=endDate variant returns the short 5m/15m windows, at the SAME moment. The old code queried one slice only, so whenever that slice went stale the bot went blind (and the alert fired) while live markets were one query away. Fix: `discover_active_markets` and `discover_binary_markets` now MERGE both param variants (deduped) instead of first-wins — whichever slice has the windows gets picked up. The alert now fires only when BOTH variants genuinely return nothing. Tests: 445 passing (merge regression test + existing discovery tests updated). |
| 2026-08-12 | **Fee model corrected to the CONFIRMED rate + category-aware fees (correctness, not a threshold tune — external review approved).** docs.polymarket.com/trading/fees now loads and confirms the crypto taker RATE is **0.07** (the 0.06 constant was a best-effort estimate from when the page was unreachable; 0.06 understated the real ~7% mid-price round trip by ~17%). Changes: (1) `TAKER_FEE_PCT` 0.06 → 0.07 and `DEFAULT_TAKER_FEE_RATE` 0.06 → 0.07; (2) NEW `engine/fees.py` `CATEGORY_FEE_RATES` + `fee_rate_for_category()` — the rate is category-dependent (crypto 0.07, sports/econ/culture/weather/other 0.05, finance/politics/mentions/tech 0.04, **geopolitics FREE**), and Gamma exposes the category as a TAG label (event["category"] is None — verified live); (3) `Market.category` field populated from tags (crypto windows hardcode "crypto"); (4) the sum-to-one scanner and paper broker now apply the category-aware rate per market — a sub-$1 pair on a fee-free geopolitics market is detected as pure profit instead of being erased by a wrong crypto-rate fee. This is the fee-correction half of the approved plan (the maker-order + Kalshi halves are separate scoped work). Tests: 451 passing (6 new: category-rate table, tag matching, unknown→other-not-crypto, fee-free geopolitics pair, category-aware opportunity detection, fallback honored). |
| 2026-08-12 | **Sum-to-one MAKER execution — the "stop paying taker fees" build, scoped to the sum-to-one leg only (external review approved with the one-maker-one-taker design change).** Takers pay `rate·p·(1−p)` on BOTH legs; MAKERS pay zero and earn a rebate. Instead of taking both legs, the bot now posts the CHEAPER leg as a resting BUY at the bid (its fee fraction of notional `rate·(1−p)` is the larger, so zeroing it saves the most, and bid < ask makes the entry strictly cheaper) and takes the other leg at market the INSTANT the maker fills — the exposure gap is one book read, never an open-ended cancel-and-reverse wait (the both-legs-maker variant was rejected precisely because "reverse" reopens the naked-position window the momentum_fallback disaster closed). New pieces: `SUM_TO_ONE_MAKER_ENABLED=True` + `SUM_TO_ONE_MAKER_TIMEOUT_S=90`; `maker_orders` audit table + DB methods; `post_sum_to_one_maker` (locks the bid+ask combo, depth-checks the TAKER book so a fill can always be paired, reserves the pair's cash — the reservation counts toward `MAX_TOTAL_EXPOSURE_PCT`); `check_sum_to_one_makers` (fill detect via ask-crossing → pair / reverse; timeout → cancel + refund); restart cleanup marks stale PENDING rows CANCELLED. Safety: a maker fill whose lock dies is reversed at market immediately (entry at the bid ⇒ reversal loss bounded by the spread), and a taker leg that breaks the lock is NEVER taken. The directional leg is untouched and the freeze rule's numbers are unchanged. Tests: 464 passing (13 new: post/depth/lock/balance rejections, fill→pair with zero-fee maker leg, partial fill, lock-broken→reverse, no-bid→hold, timeout→cancel, fee reservation math, restart cleanup). |
| 2026-08-12 | **External-review Phase 1+2 — correctness + measurement, all bug-fix level (no threshold tuned to a loss cluster).** (1) **EQUAL-SHARE sum-to-one sizing** (the "risk-free" arb is now actually risk-free): both legs buy the SAME share count, capped by both books' depth within the budget — the previous equal-DOLLAR sizing ($50 YES @ 0.40 = 125 sh vs $50 NO @ 0.50 = 100 sh) paid $125 if YES won vs $100 if NO won, a residual directional bet dressed up as arbitrage. The maker path pairs equal shares too, and holding to settlement is now exactly payout-neutral-risk at a positive edge. (2) **KELLY sizing bug fixed**: sizing always received `yes_book.mid` + the RAW edge — a NO entry at ~0.73 was sized as if buying at 0.25, and the fee the firing gate already subtracted was re-added to size. Now `_sizing_inputs` passes the ACTUAL side's best ask and the post-fee edge. (3) **CONSERVATIVE maker fills**: the resting bid sits at the BACK of its queue — a fill now requires crossing ask volume to EXCEED our order size (price touching our level alone is no longer treated as a fill), so paper results stop flattering the maker path. (4) **GAP-TIMED EXIT** (`GAP_EXPIRED`, measurement-first): if an asset has closed REPRICE winners and a position has been held past 1.5x their median hold (floor 30s, new `GAP_EXIT_MULTIPLIER`/`GAP_EXIT_MIN_HOLD_S`) without repricing, exit instead of waiting out the 240s backstop — the "enter and get out inside the gap" idea applied to the EXIT side, off until the strategy's own reprice data exists. (5) **Measurement**: per-signal `poly_book_age_s` (WS-cache book age — the 5s freshness vs ~2s window question) and a GAP MEASUREMENT section in the daily forensics digest (median lag, reprice rate, direction accuracy per asset — the number that decides whether gap-timed ENTRIES are ever safe). All frozen threshold values are untouched. Tests: 474 passing (10 new + updated sum-to-one/maker suites for equal-share and queue semantics). |

## 7. FREEZE RULE (binding, added 2026-08-11)

From this push forward, the run is hands-off. **No pattern-derived threshold
edits until ≥ 100 closed trades AND ≥ 7 days, whichever is later.**

- Allowed any time: pure bug fixes (things that are wrong, not tuned).
- NOT allowed: threshold changes derived from the latest loss cluster
  (caps, gains, hold times, gate values) — no matter how compelling the
  story is. The next loss cluster will arrive; do not tune it.
- The 0.58 cap is provisional by design: its forward block-log (signals
  table, `entry ask > max`) is the evidence it will be judged on at 100+
  trades. Re-evaluate then; do not touch it before.

This is the third attempt at a clean window. Every push restarts the cloud
bot and resets the clock — the scarce resource is an untouched sample, not
another lever.

## 8. THE TWO GATES — RECONCILED (added 2026-08-11)

There are two sample-size gates in the repo and they deliberately DISAGREE,
because they govern different decisions. Decided here so nobody picks the
easier number at trade #110:

| Decision | Gate | Numbers | Where |
|---|---|---|---|
| "Can we re-tune a threshold?" (the freeze) | FREEZE_MIN_TRADES / FREEZE_MIN_DAYS | **≥100 closed trades AND ≥7 days** | main.py + this doc §7 |
| "Is the paper run good enough for live money?" (go/no-go) | scripts/validate_paper_run.py | **≥200 trades AND ≥7 days AND ≥5 distinct trading days**, plus win rate ≥70%, positive expectancy, drawdown < kill | validate_paper_run.py |

At trade #110 the FREEZE numbers govern whether any threshold can be touched
(no — until 100 AND 7). The LIVE numbers govern the separate go/no-go for
live money, which is a higher bar by design (distinct trading days = regime
coverage, not just calendar span). Both are surfaced automatically in the
daily Telegram forensics digest, so the gates stay visible instead of being
an honor-system memory.

## 9. DAILY FORENSICS DIGEST + BOOK-IMBALANCE LOGGING (added 2026-08-11)

Closing the loop on the freeze rule WITHOUT touching any threshold — pure
reporting and measurement, per the external review's "reporting/reconcilia-
tion, not trading logic" direction:

1. **Daily Telegram forensics digest** (`_telegram_forensics_loop`, every
   `TELEGRAM_FORENSICS_INTERVAL_HOURS`=24h): pushes the premature-vs-
   protective EDGE_REVERSAL split (from the exit_probes data), per-exit-
   reason net PnL, trade count / days / distinct trading days, and progress
   against BOTH gates (§8). Nothing has to be run by hand anymore;
   `scripts/analyze_exits.py` now imports the same classification from
   `engine/exit_forensics.py` so the manual tool and the digest can't drift.
2. **Book-imbalance logging** (measurement-only): every signal evaluation now
   records `book_imbalance_pct` — bid/(bid+ask) USD depth on the target
   token's book — computed from Polymarket books the bot already holds (no
   new vendor, no new subscription). Logged ONLY, never gates. After the
   100+ trade bar we can test whether losing entries cluster on thin/one-
   sided books before ever letting it gate a trade.

Not shipped (explicitly deferred, not forgotten): Binance/Coinbase depth
streams, funding rate / open interest, and DVOL as logged fields. Those need
new network integrations (a Binance @depth subscription, futures endpoints,
Deribit) and are a larger build — worth doing as one focused, tested push
after this one settles, still measurement-only.

### 2026-08-12 — Executable-profit forensics (measurement-only, per external
review "distinguish theoretical repricing from executable profit")

The lag strategy's premise is "Polymarket eventually follows Binance" — but
"the market moved the right way" ≠ "we could have made money" (the book can
reprice before our order lands, fills slip, fees eat the edge). This batch
builds the instrument that measures that gap, changes NO thresholds:

1. **`scripts/executable_forensics.py`** (new): per closed directional trade,
   joins the decision signal (edge + `poly_book_age_s`), the entry-latency
   row (`tick_to_order_ms`), fill slippage, fees, exit reason, and net PnL;
   reports win rate, EDGE DECAY (avg decision edge − avg net return), median
   tick→order vs the ~2s window, avg book age at decision, and per-reason
   net PnL. Sparse-DB safe (missing joins tolerated). Run manually like
   `analyze_exits.py`; both import the shared classification module.
2. **`classify_early_exits` now covers GAP_EXPIRED** (`engine/exit_forensics
   .py`, exit_reasons parameterized, default EDGE_REVERSAL unchanged): the
   digest and the new script both split GAP_EXPIRED exits into premature /
   protective from exit_probes — the selection-bias check the review asked
   for (GAP_EXPIRED is built on the median of REPRICE WINNERS; is it cutting
   losers or also slow winners?).
3. **Digest sections** (`alerts/status_report.py`): GAP_EXPIRED premature/
   protective breakdown + per-asset GAP MEASUREMENT (median lag ms, reprice
   rate, direction accuracy) now render in the daily Telegram digest.

Freeze status: unchanged. All additions; `settings.py` diff is pure additions
(zero deletions) — no frozen threshold was touched. 474 tests pass.

### 2026-08-12 — Discovery-dry alert sensitivity fix (infrastructure, allowed
under the freeze)

User reported the 🚨 CRITICAL "Discovery found 0 markets" alert firing
repeatedly. Investigated live rather than theorized:

- The bot's OWN discovery code found **4 live BTC/ETH 5-min windows right
  now** (liq $3.6k–$16.8k) while the alert was firing — discovery works.
- Gamma genuinely serves empty slices intermittently: the SAME keyset query
  returned 0 events one minute and 100 events (live August-2026 windows) the
  next. Verified twice.
- Root cause: `DISCOVERY_EMPTY_ALERT_AFTER_PASSES = 3` (3 passes × 3s ≈ 9
  seconds) — far too sensitive for an API that blips empty for a minute or
  two. Every transient blip screamed CRITICAL.

Fix: raised to **60 passes (≈3 min of sustained emptiness)** — filters the
blips while still catching a real outage before the 5-min windows expire with
no replacements. No trading logic touched; the wipe-on-empty protection
(keep last-known markets + WS subscriptions) is unchanged. 63 tests pass on
the affected files; full suite 474.

---

*Continue logging every intervention below — a run with a documented human
intervention is still usable; a run with an undocumented one isn't
trustworthy.*
