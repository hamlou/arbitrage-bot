# polymarket-arb-bot

A research bot for Polymarket's short-duration (5/15-minute) BTC/ETH
up-or-down contracts, running two independent strategies:

1. **Latency/fair-value arbitrage** — watches Binance for price moves,
   computes a reference-price-aware fair value probability (not just "recent
   momentum vs. current price" — see `engine/fair_value.py`), compares it to
   Polymarket's live order-book-implied probability, and takes the side of
   the trade that gap implies when the edge and confidence clear their
   thresholds.
2. **Sum-to-one arbitrage** — checks whether YES_ask + NO_ask has fallen
   below $1 net of fees; if so, buying both sides locks in a profit
   regardless of outcome. This doesn't depend on forecasting direction at
   all (see `engine/sum_to_one.py`).

## Paper-first, on purpose

**Polymarket has no official testnet.** There's no faucet, no sandbox, no way
to send Polymarket itself a "fake" order. So instead of pretending otherwise,
this project builds its own paper-trading simulator (`engine/broker_paper.py`)
that fills orders against the **real, live order book** pulled from
`data/polymarket_feed.py` (backed by a real-time WebSocket subscription —
`data/polymarket_ws_feed.py` — not REST polling). Balance and positions are
entirely virtual — nothing here ever touches a wallet — but slippage,
spread, and fill prices reflect a real, continuously-updating book, including
a configurable simulated fill latency so the sim doesn't flatter itself with
an idealized instant fill.

That's this project's real "testnet." Treat it as such: **run it for at least
a week and 200+ trades before you even think about live mode.**

## What ships enabled by default

- `PAPER_MODE=True`
- All three `LIVE_TRADING_CONFIRMED_*` flags `False`
- No live order path is reachable from `main.py` unless all three flags are
  `True` **and** `PAPER_MODE` is explicitly `False`, simultaneously
  (`engine/broker_live.py::build_live_broker`)
- A startup assertion that hard-crashes if a real `POLYGON_PRIVATE_KEY` is
  present while `PAPER_MODE=True` — a real key has no business anywhere near
  a "risk-free" run

`LiveBroker` places real orders and reads real balances via `py-clob-client-v2`
(CLOB V2 — the only version that works against production since Polymarket's
2026-04-28 cutover). On-chain redemption of resolved positions is deliberately
left unimplemented — see that method's docstring for why.

## Project layout

```
config/settings.py       every threshold lives here, nowhere else
data/binance_feed.py      public Binance WS feed (no API key needed)
data/polymarket_feed.py   Gamma discovery + CLOB REST fallback (no credentials needed)
data/polymarket_ws_feed.py real-time order-book cache via the CLOB market-data WS channel
engine/fair_value.py      reference-price-aware probability model (primary signal source)
engine/calibration.py     momentum-continuation calibration (fallback model, fit from history)
engine/signal.py          edge/confidence computation; logs every evaluation, fired or not
engine/sum_to_one.py       risk-free combo-arbitrage detection
engine/risk.py            Kelly sizing + hard cap, exposure cap, daily halt, persistent kill switch
engine/latency.py         tick-to-order latency instrumentation
engine/broker_paper.py    the actual "testnet" — real book fills, true equity, early exits, restart recovery
engine/broker_live.py     real order placement via py-clob-client-v2, triple-flag-gated
storage/                  SQLite schema + async read/write helpers (self-migrating)
alerts/telegram.py        Telegram notifications; never crashes the loop if it fails
ui/dashboard.py           rich terminal dashboard, refreshes independently of the trading loop
scripts/validate_paper_run.py     PASS/FAIL gate, with regime-coverage warnings
scripts/calibrate_momentum_model.py  fits the fallback model from historical price data
scripts/benchmark_rpc.py          measures real latency to candidate Polygon RPC endpoints
scripts/report_latency.py         tick-to-order latency percentiles vs. the real arb window
scripts/export_csv.py             dump trades/equity to CSV
tests/                    unit + integration tests using recorded fixtures — no live network calls
```

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # defaults are already paper-safe; edit Telegram fields if you want alerts
python main.py
```

A note on network access: this needs outbound access to `stream.binance.com`,
`gamma-api.polymarket.com`, `clob.polymarket.com`, and
`wss://ws-subscriptions-clob.polymarket.com`. If you're running this
somewhere with restricted egress, make sure those are reachable — the bot
can't do anything useful without live market data.

**Production secrets:** in production, provide secrets such as
`POLYGON_PRIVATE_KEY` as an environment variable set by the deployment
platform (systemd `Environment=`/`EnvironmentFile=`, a container
orchestrator's secret store, or a managed secrets manager) — never commit
them to a checked-in `.env` file. The local `.env` (already gitignored) is
for development convenience only; anything committed to the repository is
therefore public, and a private key that ends up in git is compromised no
matter how carefully the code handles it.

## Running the tests

```bash
pytest -q
```

All tests use recorded fixture data or fake feeds (`tests/fixtures/`,
`tests/test_main_integration.py`) — none of them hit a live endpoint.

## The actual gate before you go live

Run this after a week+ of paper trading:

```bash
python scripts/validate_paper_run.py
```

It checks, against your real paper-trade history:
- ≥ 200 completed trades
- ≥ 7 calendar days elapsed
- ≥ 70% win rate
- **positive expectancy per trade after modeled fees and slippage** (not before)
- max observed drawdown < your configured kill-switch threshold

If it fails, that's the answer: keep paper trading. Don't lower the thresholds
in `config/settings.py` until the number you want appears — that's optimizing
the test, not the strategy.

## Known limitations (deliberately not built yet)

A second code review flagged a long list of production-hardening items.
Some were already outdated by the time it was written (WS feed, concurrency,
latency tracking, and calibration were already in place). The genuinely real
bugs it found — dead settlement code, in-memory-only position tracking that
didn't survive a restart, cash-only risk accounting, no exposure caps, wrong
book used for NO-side confidence scoring, an invented probability model that
ignored the contract's actual reference price — are fixed in this version;
see the git history / commit messages for specifics.

Still genuinely missing, on purpose, because they're either infra/deployment
concerns rather than strategy bugs, or need verification against live systems
this environment can't reach:

- **Order cancellation** and **on-chain redemption** of resolved live
  positions (`LiveBroker` docstrings explain why each is left unimplemented)
- **Backtesting / walk-forward validation framework** — `calibrate_momentum_model.py`
  fits the fallback model from history, but there's no full strategy backtest
- **Cross-exchange validation** (Coinbase alongside Binance), **oracle
  monitoring** (Chainlink vs. Polymarket settlement price) — both were in the
  original continuation-prompts package as separate strategy modules, not yet built
- **Production ops**: secrets management beyond `.env`, DB locking/recovery,
  health checks + auto-restart, NTP-synced clocks, idempotency keys on order
  submission, deployment redundancy
- **Reference price accuracy**: `Market.reference_price` is approximated as
  "the Binance price when our bot first saw this market," not Polymarket's
  authoritative reference price (unverified field name in Gamma's schema) —
  see `Market`'s docstring in `data/polymarket_feed.py`

## Before you ever flip `PAPER_MODE` to `False`

This isn't code, it's a checklist — go through it honestly:

- Does your expectancy stay positive if the arbitrage window is meaningfully
  shorter by the time you're live than it was during your paper run? (This
  window has been shrinking industry-wide; don't model for today's number.)
- Have you priced in Polygon gas and Polymarket's fee schedule on the
  short-duration markets specifically?
- Is your Polygon RPC endpoint fast enough to compete, or is a free/shared one
  adding latency your paper run never accounted for?
- Can you actually afford to lose the entire amount you're about to deposit,
  with zero effect on anything else in your life? If the honest answer is no,
  that's a signal to stay in paper mode longer — not a signal to lower the
  thresholds until the numbers say what you want them to say.

None of this is financial advice — it's an engineering risk checklist, same
as the one that shipped with the original prompt set this project was built
from.
