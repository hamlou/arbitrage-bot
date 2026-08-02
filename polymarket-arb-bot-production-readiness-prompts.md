# Polymarket Arb Bot — Production-Readiness Package (Paper-Test-Ready)

**Read this first.**

- This continues the project as it now stands: fair-value probability model, sum-to-one
  arbitrage, WS order-book feed, true mark-to-market equity, exposure caps, restart recovery,
  direct settlement polling, early exits, latency instrumentation, calibration tooling. 93
  tests passing, all against fixtures/fakes — **none of it has touched a live endpoint**, because
  the environment that built it has no network access to Binance or Polymarket.
- Your job with this package is different from the earlier ones: it's not "add a feature," it's
  "verify everything that was built blind actually works against the real thing, then close the
  handful of genuinely missing pieces." Expect the first few prompts to surface real bugs —
  wrong field names, schema drift, rate limits — that's the point of running them.
- Paste these in order. Run and read the output of each before moving to the next. When
  something errors against a real API, paste the actual error/response back before continuing —
  don't let it guess.

---

## Prompt 1 — Live verification pass (do this before anything else)

```
Nothing in this codebase has been tested against a real network connection. Before adding
any new code, verify what's already here against reality:

1. Run scripts/smoke_test_feeds.py if it exists; if not, write it: connect to the real Binance
   WS for 30s and print ticks, call the real Gamma API and print 3 discovered markets' parsed
   fields, call the real CLOB /book endpoint for one of them and print best bid/ask.

2. Specifically check data/polymarket_feed.py's _parse_gamma_market(): does Gamma's actual
   response contain a usable reference/strike/open price field for these BTC/ETH up-down
   markets? Market.reference_price currently has NO real Gamma field wired in — it's
   approximated as "the Binance price when our bot first discovered this market" (see the
   docstring on Market in data/polymarket_feed.py and the comment in
   main.py::_market_discovery_loop). Find the actual field if Gamma exposes one (check the
   full raw JSON response, not just the fields we're already parsing) and wire it in properly,
   replacing the approximation. If Gamma genuinely doesn't expose it, confirm that and leave
   the approximation in place but note the finding.

3. Verify data/polymarket_ws_feed.py's message schema (event_type: book / price_change /
   best_bid_ask etc.) against what the real WS connection actually sends. Fix any mismatches.

4. Verify engine/broker_live.py's assumptions about py_clob_client_v2's response shape from
   create_and_post_market_order() — the field names used to build LiveFill (price, size, fee,
   orderID) are marked in that file as "best-effort, not independently verified." Confirm or
   correct them using a real (or sandboxed/dry-run if the SDK offers one) call.

5. Run the full pytest suite again after any fixes and confirm nothing regressed.

Do not skip to later prompts until this one is clean — everything else assumes the data layer
is actually correct, not just internally consistent.
```

## Prompt 2 — Order cancellation

```
engine/broker_live.py can place orders but not cancel them. Using py_clob_client_v2's real
cancellation methods (inspect the installed package directly — python3 -c "from
py_clob_client_v2 import ClobClient; help(ClobClient.cancel_order)" or equivalent — don't
guess the signature), implement:

- LiveBroker.cancel_order(order_id: str) -> bool
- LiveBroker.cancel_all_orders(market_id: str | None = None) -> int

Wire a cancel path into engine/broker_paper.py's PaperBroker too (paper orders currently fill
immediately via FOK simulation, so "cancel" mostly matters for any future resting/limit-order
support — e.g. the market-making strategy from the earlier continuation-prompts package — but
add the interface now so both brokers stay symmetric).

Add unit tests using a mocked py_clob_client_v2 client (don't cancel real orders in tests).
```

## Prompt 3 — On-chain redemption (the one deliberately-unfinished piece)

```
engine/broker_live.py::settle_position raises NotImplementedError on purpose — the previous
build stage wouldn't guess at the CTF contract's redemption ABI without verification, since a
wrong contract call risks real funds. Your job now:

1. Find Polymarket's current CTF (Conditional Tokens Framework) contract address and ABI for
   redeeming resolved positions on Polygon — check docs.polymarket.com and/or the contract
   verified on Polygonscan directly. Confirm this against the ACTUAL currently-deployed
   contract, not a cached/remembered address, since this project already found one hard
   breaking change (the V1->V2 CLOB migration) that invalidated an earlier assumption.
2. Implement LiveBroker.redeem_position(market, trade) using web3.py (add as a dependency)
   to call the verified redeemPositions (or equivalent) function.
3. This is real-money code. Before wiring it into the automatic settlement loop, add a
   standalone script (scripts/manual_redeem.py) that redeems ONE specific resolved position
   given its market ID, prints the transaction hash, and waits for confirmation — run this
   manually against a tiny resolved position first. Only wire it into the automatic loop
   (main.py's _settlement_loop) after that manual test succeeds.
```

## Prompt 4 — Backtesting / walk-forward validation

```
scripts/calibrate_momentum_model.py fits the FALLBACK momentum model from historical price
data, but there's no backtest of the actual strategy end-to-end (fair-value model + signal
thresholds + Kelly sizing + fees + slippage) against historical data.

Build scripts/backtest.py that:
- Takes historical Binance price data (same input format as calibrate_momentum_model.py) and,
  ideally, historical Polymarket order-book snapshots if you can source them (check whether
  Polymarket or a third party publishes historical CLOB data — if not, note that limitation
  explicitly rather than fabricating synthetic order books that flatter the strategy).
- Replays engine/fair_value.py + engine/signal.py's exact logic (import and reuse the real
  modules, don't reimplement the math separately where it could drift from the live code) against
  that historical data, simulating fills the same way engine/broker_paper.py does.
- Reports the same metrics scripts/validate_paper_run.py does (win rate, expectancy net of
  fees/slippage, max drawdown), split by volatility regime/time period so a single lucky
  stretch doesn't dominate the result.
- Supports walk-forward validation: fit calibration on period N, test on period N+1, roll
  forward — report whether the edge holds up out-of-sample or was overfit to the fitting window.
```

## Prompt 5 — Cross-exchange validation

```
The fair-value model currently trusts a single Binance price feed completely. Add
data/coinbase_feed.py (same public-WS-no-auth pattern as data/binance_feed.py) for BTC-USD/
ETH-USD, and in engine/signal.py, before firing a signal, check that Binance and Coinbase agree
within a small tolerance (configurable, e.g. 0.1%). If they diverge more than that, treat it as
a data-quality problem (possible feed lag/glitch on one exchange) and skip firing that cycle
rather than trading on a potentially bad price. Log disagreements to a new table
(exchange_disagreements) for later review of how often this actually happens.
```

## Prompt 6 — Production operations hardening

```
Close the remaining gaps from the "production ops" list in README.md's Known Limitations
section:

1. Health check + auto-restart: add a simple systemd service file (or supervisor config) that
   restarts main.py on crash, with a backoff so a persistent crash-loop doesn't hammer the
   APIs. Log crash reasons somewhere the operator will actually see them (the existing Telegram
   alerter already fires on unhandled trading-cycle errors — make sure a process-level crash
   also alerts, not just in-loop exceptions).
2. Secrets: confirm POLYGON_PRIVATE_KEY is never written to any log file, crash dump, or the
   SQLite DB anywhere in the codebase (grep for it). Document the intended production secrets
   flow (e.g. environment injection via the deployment platform, not a checked-in .env).
3. Idempotency: add an idempotency key to LiveBroker.place_order so a retried call (e.g. after
   a network timeout where you don't know if the original order landed) can't accidentally
   double-submit. Check whether py_clob_client_v2 supports a client-supplied order ID/nonce for
   this; if not, implement a local dedupe check against recent orders before submitting.
4. DB durability: confirm SQLite is running with WAL mode enabled (storage/db.py) so a crash
   mid-write doesn't corrupt the database, and add a scripts/db_integrity_check.py that runs
   PRAGMA integrity_check and can be scheduled as a periodic health check.
```

## Prompt 7 — Full paper-validation run

```
With Prompts 1-6 done, run the bot in paper mode continuously for at least the 7 days / 200
trades scripts/validate_paper_run.py requires — ideally spanning more than one volatility
regime, per that script's regime-coverage warning. Do not touch any LIVE_TRADING_CONFIRMED_*
flag until:

- scripts/validate_paper_run.py reports PASS with no regime-concentration warnings
- scripts/report_latency.py shows p95 tick-to-order latency comfortably under whatever the
  current real arbitrage window turns out to be (re-verify that window empirically — the 2.7s
  figure this project has been using throughout came from a single secondhand article and
  should not be trusted as current; measure your own observed Polymarket repricing delay
  during the paper run instead)
- You've manually reviewed a sample of sum_to_one trades and fair_value-model trades separately
  in the trades table (filter by the `strategy` column) to confirm both are behaving sensibly,
  not just that the aggregate numbers look fine
```

---

## What's deliberately NOT in this package

- **Oracle arbitrage, news-driven trading, market making, wallet copy-trading** — these were
  already scoped as separate strategy modules in the earlier continuation-prompts package.
  Add them only after the core two strategies (fair-value + sum-to-one) have a real paper
  track record — bolting on more strategies before the first two are validated just adds more
  unvalidated surface area.
- **Any recommendation of a specific RPC provider, hosting region, or "guaranteed" latency
  number.** Measure these yourself with scripts/benchmark_rpc.py from wherever you actually
  deploy — a claim like "Polymarket's servers are in AWS us-east-1, expect <5ms" showed up in
  an AI-generated suggestion during this project's planning and could not be verified from
  any source; don't build infrastructure decisions around unverified specifics like that.
