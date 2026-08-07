# Arb OS — Command Center

The brain of the Polymarket arbitrage bot, exposed. A web application that turns
the bot's entire state — trades, positions, markets, latency, risk, signals —
into a live operational dashboard.

## Architecture

```
polymarket-arb-bot/
├── main.py                         # the bot — now also exports live state
└── command_center/
    ├── api/main.py                 # FastAPI read-only backend (port 8787)
    │                              #  - reads storage/arb_bot.db (SQLite)
    │                              #  - reads live_state.json (written by the bot)
    │                              #  - GET /api/* + WS /ws/live
    ├── start_api.bat               # double-click to start the API
    ├── start_ui.bat                # double-click to start the UI
    └── ui/                         # Next.js 15 + TypeScript + Tailwind
        └── app/                    # Overview, Trades, Positions, Markets,
                                    # Latency, Activity, Risk & Config
```

Two processes, zero coupling:

1. **The bot** (`python main.py`) keeps doing what it always did. One new
   background loop writes `command_center/api/live_state.json` every ~2s —
   a pure side effect; if it fails, trading is untouched.
2. **The API** (`start_api.bat`) is read-only. It never writes to the bot's
   database. When the bot is offline the dashboard degrades gracefully
   (BOT OFFLINE badge) while still showing all historical data from SQLite.

The Next.js dev server proxies `/api/*` to the FastAPI backend (see
`ui/next.config.mjs`), so the browser only ever talks to one origin.

## Running it

1. Start the bot: double-click `start_paper.bat` (from the bot folder).
2. Start the API: double-click `command_center/start_api.bat`.
3. Start the UI: double-click `command_center/start_ui.bat`
   (installs dependencies on first run).
4. Open **http://localhost:3000**

## API reference

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | API + bot liveness |
| `GET /api/overview` | Everything the home screen needs |
| `GET /api/trades` | Trade ledger with filters |
| `GET /api/positions` | Open positions, marked to market |
| `GET /api/equity` | Equity curve |
| `GET /api/signals` | Model reads (fired + not fired) |
| `GET /api/latency` | Timing percentiles + series |
| `GET /api/risk-events` | Halt / kill-switch history |
| `GET /api/disagreements` | Binance↔Coinbase divergences |
| `GET /api/activity` | Unified decision timeline |
| `GET /api/config` | Settings snapshot (no secrets) |
| `WS /ws/live` | 2-second live snapshots |

Interactive docs: http://127.0.0.1:8787/docs

## Keyboard

- `⌘K` / `Ctrl+K` — command palette
- `Esc` — close panels

## Design system

Dark-first trading terminal. All colors are CSS variables in
`ui/app/globals.css`; `ui/tailwind.config.ts` maps them to Tailwind tokens.
Light mode is a single `.light` class on `<html>`. Numbers always use
tabular figures (`.tabular`). No component has theme logic — the tokens flip.
