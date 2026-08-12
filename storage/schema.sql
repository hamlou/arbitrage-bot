-- polymarket-arb-bot storage schema
-- Every evaluated opportunity is logged, not just executed trades, so we can
-- audit false negatives (edges that existed but didn't meet threshold/confidence).

CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,          -- unix epoch seconds
    market_id       TEXT    NOT NULL,
    asset           TEXT    NOT NULL,          -- BTC / ETH
    implied_prob    REAL    NOT NULL,          -- from Binance-derived momentum
    polymarket_prob REAL    NOT NULL,          -- from live order book mid
    edge_pct        REAL    NOT NULL,
    confidence      REAL    NOT NULL,
    fired           INTEGER NOT NULL,          -- 0/1, did it pass thresholds
    reason          TEXT,                      -- why it fired or didn't
    binance_tick_age_s REAL,
    book_depth_usd  REAL,
    book_imbalance_pct REAL,                -- bid/(bid+ask) USD depth @5 levels on the target book (measurement-only, never gates)
    poly_book_age_s  REAL                   -- age of the Polymarket book evaluated against (WS cache age; measurement-only)
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signals_market ON signals(market_id);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id       INTEGER REFERENCES signals(id),
    market_id       TEXT    NOT NULL,
    asset           TEXT    NOT NULL,
    side            TEXT    NOT NULL,          -- YES / NO
    mode            TEXT    NOT NULL,          -- PAPER / LIVE
    strategy        TEXT    NOT NULL DEFAULT 'latency_arb',  -- latency_arb / sum_to_one / ...
    combo_group_id  TEXT,                      -- links paired legs (e.g. sum-to-one's YES+NO pair); NULL for single-leg trades
    entry_ts        REAL    NOT NULL,
    entry_price     REAL    NOT NULL,
    size_usd        REAL    NOT NULL,
    fee_usd         REAL    NOT NULL DEFAULT 0,
    slippage_pct    REAL,                      -- (avg_fill - mid_at_decision) / mid_at_decision
    decision_best_ask REAL,                    -- best ask at decision time (edge-decay measurement)
    fill_best_ask   REAL,                      -- best ask at fill time (edge-decay measurement)
    exit_ts         REAL,
    exit_price      REAL,
    exit_reason     TEXT,                      -- SETTLED / MANUAL_EXIT / TAKE_PROFIT / EDGE_REVERSAL / ...
    realized_pnl_usd REAL,
    status          TEXT    NOT NULL DEFAULT 'OPEN',  -- OPEN / CLOSED
    mfe_pct         REAL,                      -- max favorable excursion, fraction vs entry (measurement only)
    mae_pct         REAL                       -- max adverse excursion, fraction vs entry (negative; measurement only)
);
CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_id);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_combo_group ON trades(combo_group_id);

-- Resting maker orders for the sum-to-one leg (added 2026-08-12): one
-- leg posts at the bid (maker = zero fee + rebate), the other is taken the
-- instant the maker leg fills. Audit trail only — the live lifecycle lives
-- in the paper broker's in-memory registry (a resting order dies with the
-- process, like a real broker connection).
CREATE TABLE IF NOT EXISTS maker_orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,          -- posted (unix epoch seconds)
    market_id       TEXT    NOT NULL,
    side            TEXT    NOT NULL,          -- YES / NO — the posted (maker) leg
    token_id        TEXT    NOT NULL,
    price           REAL    NOT NULL,          -- resting limit price (best bid at post)
    size_usd        REAL    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'PENDING',  -- PENDING / FILLED / REVERSED / CANCELLED
    filled_price    REAL,                      -- maker leg fill price (= resting price)
    taker_leg_price REAL,                      -- the taken leg's avg fill at pair open
    combined_cost   REAL,                      -- filled_price + taker_leg_price (before fees)
    taker_fee_usd   REAL,                      -- fee on the taken leg only (maker leg = 0)
    combo_group_id  TEXT,
    notes           TEXT,
    resolved_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_maker_orders_status ON maker_orders(status);
CREATE INDEX IF NOT EXISTS idx_maker_orders_market ON maker_orders(market_id);

CREATE TABLE IF NOT EXISTS equity_curve (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    mode            TEXT    NOT NULL,          -- PAPER / LIVE
    balance_usd     REAL    NOT NULL,
    unrealized_pnl_usd REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_curve(ts);

CREATE TABLE IF NOT EXISTS risk_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    event_type      TEXT    NOT NULL,          -- DAILY_HALT / KILL_SWITCH / MANUAL_RESET
    detail          TEXT,
    balance_usd     REAL,
    drawdown_pct    REAL
);
CREATE INDEX IF NOT EXISTS idx_risk_events_ts ON risk_events(ts);

-- Every stage of the tick -> signal -> (would-be) order pipeline is timestamped
-- so end-to-end latency can be measured against the actual arbitrage window,
-- not assumed. See engine/latency.py and scripts/report_latency.py.
CREATE TABLE IF NOT EXISTS latency_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id           TEXT    NOT NULL,
    tick_received_at    REAL    NOT NULL,   -- when the triggering Binance tick was received locally
    signal_evaluated_at REAL    NOT NULL,   -- when SignalEngine.evaluate() finished for this cycle
    order_submitted_at  REAL,               -- when place_order() was called (NULL if signal didn't fire)
    tick_to_signal_ms   REAL    NOT NULL,
    signal_to_order_ms  REAL,
    tick_to_order_ms    REAL,
    fired               INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_latency_events_market ON latency_events(market_id);

-- Every observed cross-exchange price disagreement above
-- CROSS_EXCHANGE_TOLERANCE_PCT is recorded here — whether or not it blocked a
-- signal — so disagreement frequency can be reviewed later (see the
-- cross-exchange gate in engine/signal.py).
CREATE TABLE IF NOT EXISTS exchange_disagreements (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               REAL    NOT NULL,          -- unix epoch seconds
    symbol           TEXT    NOT NULL,          -- e.g. BTCUSDT
    binance_price    REAL    NOT NULL,
    coinbase_price   REAL    NOT NULL,
    disagreement_pct REAL    NOT NULL           -- |coinbase - binance| / binance * 100
);
CREATE INDEX IF NOT EXISTS idx_exchange_disagreements_ts ON exchange_disagreements(ts);

-- Empirical arbitrage-window measurement: for every Binance move above
-- LAG_TRACK_MOVE_MIN_PCT, how long did Polymarket take to reprice the
-- direction-implied token (if it repriced at all). Filled by
-- engine/lag_tracker.py via main.py's _lag_tracker_loop. This is the
-- measured lag on THIS connection — the number ASSUMED_ARBITRAGE_WINDOW_S
-- currently guesses at.
CREATE TABLE IF NOT EXISTS lag_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               REAL    NOT NULL,          -- when the measurement was recorded
    asset            TEXT    NOT NULL,          -- BTC / ETH
    move_pct         REAL    NOT NULL,          -- |Binance move| that triggered (fraction)
    move_dir         TEXT    NOT NULL,          -- UP / DOWN (direction of the Binance move)
    token_id         TEXT    NOT NULL,          -- the direction-implied token that should reprice
    binance_move_ts  REAL    NOT NULL,          -- when the Binance move was received locally
    baseline_mid     REAL,                      -- implied token mid at move time
    poly_repriced_ts REAL,                      -- when the mid first moved >= LAG_REPRICE_MIN_MOVE
    poly_move_pct    REAL,                      -- actual mid change at detection (can be negative)
    lag_ms           REAL,                      -- (poly_repriced_ts - binance_move_ts) * 1000; NULL if timed out
    timed_out        INTEGER NOT NULL DEFAULT 0 -- 1 = never repriced within LAG_TRACK_TIMEOUT_S
);
CREATE INDEX IF NOT EXISTS idx_lag_events_ts ON lag_events(ts);

-- Post-exit price probes (added 2026-08-11, measurement only): after an
-- early exit (REPRICE / TAKE_PROFIT / EDGE_REVERSAL), the bot samples the
-- held token's price at T+5/15/30/60/120s and at settlement. This is the
-- premature-exit detector: did the market reprice to a win AFTER we left?
-- Filled by main.py's _exit_probe_loop; consumed by scripts/analyze_exits.py.
CREATE TABLE IF NOT EXISTS exit_probes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id     INTEGER NOT NULL REFERENCES trades(id),
    ts           REAL    NOT NULL,             -- unix epoch seconds of the sample
    sample_label TEXT    NOT NULL,             -- P_EXIT / P_5S / P_15S / P_30S / P_60S / P_120S / P_SETTLED
    quote_price  REAL    NOT NULL,             -- mid of the HELD token at sample time (1.0/0.0 at settlement)
    entry_price  REAL    NOT NULL,             -- denormalized so recovery = (quote - entry)/entry
    outcome      TEXT                          -- market outcome at settlement (YES/NO), else NULL
);
CREATE INDEX IF NOT EXISTS idx_exit_probes_trade ON exit_probes(trade_id);
