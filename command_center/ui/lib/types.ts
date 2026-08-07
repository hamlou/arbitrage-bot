export type FeedStatus = "healthy" | "stale" | "down";

export interface FeedHealth {
  healthy: boolean | null; // null = unknown (bot offline), never conflated with "down"
  reconnects_10m: number;
  stale_s: number | null;
}

export interface Account {
  mode: string;
  balance_usd: number | null;
  equity_usd: number | null;
  total_pnl_usd: number;
  win_rate_pct: number | null;
  closed_trades: number;
  open_positions: number;
  wins: number;
  losses: number;
  avg_win_usd: number | null;
  avg_loss_usd: number | null;
  profit_factor: number | null;
  uptime_s: number | null;
  paused: boolean;
  alerts_muted: boolean;
  daily_halted: boolean;
  kill_switch_tripped: boolean;
  daily_pnl_pct: number | null;
  drawdown_pct: number | null;
}

export interface RiskState {
  daily_halted: boolean;
  kill_switch_tripped: boolean;
  daily_pnl_pct: number | null;
  drawdown_pct: number | null;
  daily_halt_threshold_pct: number | null;
  kill_threshold_pct: number | null;
}

export interface LatencySummary {
  tick_to_signal_p50_ms?: number | null;
  tick_to_signal_p95_ms?: number | null;
  tick_to_order_p50_ms?: number | null;
  tick_to_order_p95_ms?: number | null;
  signal_to_order_p95_ms?: number | null;
  cycles: number;
  fired: number;
  platform_delay_ms?: number | null;
  window_s?: number | null;
  verdict?: string;
}

export interface StrategyStat {
  strategy: string;
  trades: number;
  pnl_usd: number;
  wins: number;
  losses: number;
  win_rate_pct: number | null;
  avg_pnl_usd: number | null;
}

export interface MarketRow {
  market_id: string;
  asset: string;
  duration_minutes: number;
  question: string;
  liquidity_usd: number;
  expires_at_ts: number | null;
  time_remaining_s: number | null;
  reference_price: number | null;
  yes_mid: number | null;
  no_mid: number | null;
}

export interface PositionRow {
  trade_id: number;
  market_id: string;
  side: "YES" | "NO";
  asset: string;
  entry_price: number;
  size_usd: number;
  fee_usd: number;
  strategy: string;
  mark_price: number;
  unrealized_pnl_usd: number;
}

export interface Overview {
  ts: number;
  bot_online: boolean;
  state_age_s: number | null;
  account: Account;
  feeds: { binance: FeedHealth; polymarket: FeedHealth };
  risk: RiskState;
  latency: LatencySummary;
  strategy: StrategyStat[];
  signals_today: { total: number; fired: number };
  recent_trades: Trade[];
  markets: MarketRow[];
  positions: PositionRow[];
}

export interface Trade {
  id: number;
  market_id: string;
  asset: string;
  side: string;
  mode: string;
  strategy: string;
  combo_group_id: string | null;
  entry_ts: number;
  entry_price: number;
  size_usd: number;
  fee_usd: number;
  exit_ts: number | null;
  exit_price: number | null;
  exit_reason: string | null;
  realized_pnl_usd: number | null;
  status: "OPEN" | "CLOSED";
}

export interface TradesResponse {
  trades: Trade[];
  stats: { count: number; total: number; closed_pnl_usd: number; wins: number; losses: number };
  limit: number;
  offset: number;
}

export interface EquityPoint {
  ts: number;
  mode: string;
  balance_usd: number;
  unrealized_pnl_usd: number;
}

export interface SignalRow {
  id: number;
  ts: number;
  market_id: string;
  asset: string;
  implied_prob: number;
  polymarket_prob: number;
  edge_pct: number;
  confidence: number;
  fired: boolean;
  reason: string;
  binance_tick_age_s: number | null;
  book_depth_usd: number | null;
}

export interface ActivityItem {
  ts: number;
  type: "trade" | "signal" | "risk";
  kind: string;
  label: string;
  pnl_usd?: number | null;
  edge_pct?: number | null;
  confidence?: number | null;
  detail?: string | null;
  exit_reason?: string | null;
  market_id?: string | null;
  strategy?: string | null;
  size_usd?: number | null;
  entry_price?: number | null;
  drawdown_pct?: number | null;
}
