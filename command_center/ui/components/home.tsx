"use client";

import * as React from "react";
import Link from "next/link";
import { motion } from "framer-motion";
import {
  Activity,
  ArrowRight,
  Briefcase,
  CandlestickChart,
  Gauge,
  ShieldAlert,
  Timer,
  TrendingUp,
  Wallet,
  Zap,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { Overview, EquityPoint, PositionRow } from "@/lib/types";
import { useActivity, useEquity } from "@/lib/api";
import {
  fmtDuration,
  fmtNum,
  fmtPct,
  fmtTime,
  fmtUsd,
  pnlColor,
} from "@/lib/format";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  Progress,
  Skeleton,
  Separator,
} from "@/components/ui/primitives";
import { LatencyBudget } from "@/components/charts";
import {
  EmptyState,
  LiveDot,
  PnlText,
  StatCard,
  StatusPill,
  useTick,
} from "@/components/widgets";

const stagger = {
  hidden: { opacity: 0, y: 8 },
  show: (i: number) => ({
    opacity: 1,
    y: 0,
    transition: { delay: i * 0.05, duration: 0.4, ease: [0.16, 1, 0.3, 1] as const },
  }),
};

/* ------------------------------------------------------------------ */
/* Stat strip                                                          */
/* ------------------------------------------------------------------ */

export function StatStrip({ ov, equity }: { ov: Overview; equity: EquityPoint[] }) {
  const a = ov.account;
  const spark = equity.map((e) => e.balance_usd).slice(-40);
  const winTone =
    a.win_rate_pct === null ? "neutral" : a.win_rate_pct >= 50 ? ("ok" as const) : ("danger" as const);

  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
      <StatCard
        label="Equity"
        value={a.equity_usd ?? 0}
        display={(v) => fmtUsd(v)}
        delta={a.total_pnl_usd}
        spark={spark}
        sparkColor="var(--brand)"
        icon={<Wallet />}
        footer={
          <span className={pnlColor(a.total_pnl_usd)}>
            {fmtUsd(a.total_pnl_usd, { sign: true })} all-time
          </span>
        }
      />
      <StatCard
        label="Cash"
        value={a.balance_usd ?? 0}
        display={(v) => fmtUsd(v)}
        icon={<TrendingUp />}
        footer={`${a.open_positions} open · ${a.closed_trades} closed`}
      />
      <StatCard
        label="Total PnL"
        value={a.total_pnl_usd}
        display={(v) => fmtUsd(v, { sign: true })}
        deltaTone={a.total_pnl_usd > 0 ? "ok" : "danger"}
        icon={<Zap />}
        footer={
          <span>
            avg win {fmtUsd(a.avg_win_usd)} · avg loss {fmtUsd(a.avg_loss_usd)}
          </span>
        }
      />
      <StatCard
        label="Win rate"
        value={a.win_rate_pct ?? 0}
        display={(v) => fmtPct(v)}
        deltaTone={winTone}
        icon={<Gauge />}
        footer={`${a.wins}W · ${a.losses}L`}
      />
      <StatCard
        label="Open positions"
        value={a.open_positions}
        display={(v) => v.toFixed(0)}
        icon={<Briefcase />}
        footer="across all markets"
      />
      <StatCard
        label="Profit factor"
        value={a.profit_factor ?? 0}
        display={(v) => v.toFixed(2)}
        deltaTone={
          a.profit_factor === null || a.profit_factor === undefined
            ? "neutral"
            : a.profit_factor >= 1
              ? "ok"
              : "danger"
        }
        icon={<ShieldAlert />}
        footer="gross wins ÷ gross losses"
      />
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Health + risk                                                        */
/* ------------------------------------------------------------------ */

export function HealthPanel({ ov }: { ov: Overview }) {
  const r = ov.risk;
  const dailyPct = (r.daily_pnl_pct ?? 0) * 100;
  const dailyThreshold = (r.daily_halt_threshold_pct ?? 0.2) * 100;
  const drawdownPct = (r.drawdown_pct ?? 0) * 100;
  const drawdownThreshold = (r.kill_threshold_pct ?? 0.4) * 100;

  return (
    <Card className="flex h-full flex-col">
      <CardHeader
        title="Health & Risk"
        subtitle="Feed liveness and the trading gates that protect capital"
        icon={<Activity />}
        action={
          <div className="flex items-center gap-2">
            <StatusPill healthy={ov.feeds.binance.healthy} label="Binance" />
            <StatusPill healthy={ov.feeds.polymarket.healthy} label="Polymarket" />
          </div>
        }
      />
      <div className="flex flex-1 flex-col gap-4 px-4 pb-4 pt-3">
        <div className="grid grid-cols-2 gap-3">
          <FeedCell
            name="Binance"
            health={ov.feeds.binance}
            detail={`${fmtNum(ov.feeds.binance.reconnects_10m, 0)} reconnects/10m`}
          />
          <FeedCell
            name="Polymarket"
            health={ov.feeds.polymarket}
            detail={`${fmtNum(ov.feeds.polymarket.reconnects_10m, 0)} reconnects/10m`}
          />
        </div>

        <Separator />

        <div className="space-y-3">
          <div>
            <div className="mb-1 flex items-center justify-between text-[11px]">
              <span className="text-ink-muted">Daily PnL</span>
              <span className={cn("font-mono tabular", dailyPct >= 0 ? "text-ok" : "text-danger")}>
                {dailyPct >= 0 ? "+" : ""}
                {dailyPct.toFixed(2)}% / −{dailyThreshold.toFixed(0)}% halt
              </span>
            </div>
            <Progress value={Math.max(0, dailyPct)} tone={dailyPct < -10 ? "danger" : dailyPct < 0 ? "warn" : "ok"} />
          </div>
          <div>
            <div className="mb-1 flex items-center justify-between text-[11px]">
              <span className="text-ink-muted">Drawdown</span>
              <span className={cn("font-mono tabular", drawdownPct >= drawdownThreshold * 0.7 ? "text-danger" : "text-ink-muted")}>
                −{drawdownPct.toFixed(2)}% / −{drawdownThreshold.toFixed(0)}% kill
              </span>
            </div>
            <Progress value={drawdownPct} tone={drawdownPct > drawdownThreshold * 0.7 ? "danger" : "neutral"} />
          </div>
        </div>

        <div className="mt-auto grid grid-cols-2 gap-2">
          <GateChip label="Daily halt" active={r.daily_halted} />
          <GateChip label="Kill switch" active={r.kill_switch_tripped} />
          <GateChip label="Paused" active={ov.account.paused} />
          <GateChip label="Alerts muted" active={ov.account.alerts_muted} warn />
        </div>

        <div className="flex items-center justify-between rounded-lg border border-line bg-raised px-3 py-2">
          <span className="text-[11px] text-ink-muted">Signals today</span>
          <span className="font-mono text-xs tabular text-ink">
            <span className="text-brand">{ov.signals_today.fired}</span>
            <span className="text-ink-faint"> / {ov.signals_today.total} fired</span>
          </span>
        </div>
      </div>
    </Card>
  );
}

function FeedCell({
  name,
  health,
  detail,
}: {
  name: string;
  health: { healthy: boolean | null; reconnects_10m: number; stale_s: number | null };
  detail: string;
}) {
  const status = health.healthy === null ? "unknown" : health.healthy ? "healthy" : "down";
  const tone = health.healthy === null ? "muted" : health.healthy ? "ok" : "danger";
  return (
    <div className="rounded-lg border border-line bg-raised p-3">
      <div className="flex items-center justify-between">
        <span className="flex items-center gap-1.5 text-xs font-medium text-ink">
          <LiveDot tone={tone} pulse={health.healthy === true} />
          {name}
        </span>
        <span
          className={cn(
            "text-[10px] font-semibold uppercase tracking-wider",
            tone === "ok" ? "text-ok" : tone === "danger" ? "text-danger" : "text-ink-faint"
          )}
        >
          {status}
        </span>
      </div>
      <div className="mt-1 font-mono text-[10px] text-ink-faint tabular">{detail}</div>
    </div>
  );
}

function GateChip({ label, active, warn }: { label: string; active: boolean; warn?: boolean }) {
  return (
    <div
      className={cn(
        "flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[11px]",
        active
          ? warn
            ? "border-warn/30 bg-warn-soft text-warn"
            : "border-danger/30 bg-danger-soft text-danger"
          : "border-line bg-raised text-ink-faint"
      )}
    >
      <span className={cn("h-1.5 w-1.5 rounded-full", active ? (warn ? "bg-warn" : "bg-danger") : "bg-ink-faint/40")} />
      {label}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Markets                                                              */
/* ------------------------------------------------------------------ */

export function MarketsPanel({ ov }: { ov: Overview }) {
  const now = useTick(1000);
  const markets = ov.markets ?? [];

  return (
    <Card>
      <CardHeader
        title="Live Markets"
        subtitle="Current up/down windows — mid prices stream from the order books"
        icon={<CandlestickChart />}
        action={
          <Link href="/markets">
            <Button variant="ghost" size="xs">
              View all <ArrowRight className="h-3 w-3" />
            </Button>
          </Link>
        }
      />
      {markets.length === 0 ? (
        <EmptyState
          icon={<CandlestickChart />}
          title="No live windows"
          desc="Discovery found nothing right now — new 5/15-minute windows roll every few minutes."
        />
      ) : (
        <div className="overflow-x-auto px-1 pb-2 pt-1">
          <table className="w-full min-w-[560px] border-collapse text-[12px]">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-wider text-ink-faint">
                <th className="px-3 py-2 font-medium">Asset</th>
                <th className="px-3 py-2 font-medium">Window</th>
                <th className="px-3 py-2 font-medium">Closes in</th>
                <th className="px-3 py-2 text-right font-medium">YES mid</th>
                <th className="px-3 py-2 text-right font-medium">NO mid</th>
                <th className="px-3 py-2 text-right font-medium">Liquidity</th>
              </tr>
            </thead>
            <tbody>
              {markets.slice(0, 8).map((m, i) => {
                const remaining =
                  m.time_remaining_s !== null && m.time_remaining_s !== undefined
                    ? m.time_remaining_s
                    : m.expires_at_ts
                      ? Math.max(0, m.expires_at_ts - now / 1000)
                      : null;
                const sum = (m.yes_mid ?? 0) + (m.no_mid ?? 0);
                const stoEdge = sum > 0 ? Math.max(0, (1 - sum) * 100) : 0;
                return (
                  <motion.tr
                    key={m.market_id}
                    className="border-t border-line/60 transition-colors hover:bg-raised/60"
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    transition={{ delay: i * 0.03 }}
                  >
                    <td className="px-3 py-2.5">
                      <span className="inline-flex items-center gap-1.5 font-mono text-xs font-semibold text-ink">
                        <span className="flex h-5 w-5 items-center justify-center rounded bg-brand-soft text-[9px] font-bold text-brand">
                          {m.asset.slice(0, 1)}
                        </span>
                        {m.asset}
                      </span>
                      <Badge tone={m.duration_minutes === 5 ? "info" : "brand"} className="ml-2">
                        {m.duration_minutes}m
                      </Badge>
                    </td>
                    <td className="max-w-[220px] truncate px-3 py-2.5 text-ink-muted" title={m.question}>
                      {m.question.split("—").pop()?.trim() ?? m.question}
                    </td>
                    <td className="px-3 py-2.5 font-mono tabular text-ink">
                      {remaining !== null ? fmtDuration(remaining) : "—"}
                    </td>
                    <td className="px-3 py-2.5 text-right font-mono tabular text-ok">
                      {m.yes_mid !== null ? m.yes_mid.toFixed(2) : "—"}
                    </td>
                    <td className="px-3 py-2.5 text-right font-mono tabular text-danger">
                      {m.no_mid !== null ? m.no_mid.toFixed(2) : "—"}
                    </td>
                    <td className="px-3 py-2.5 text-right">
                      <div className="flex items-center justify-end gap-1.5">
                        <span className="font-mono tabular text-ink-muted">{fmtUsd(m.liquidity_usd)}</span>
                        {stoEdge > 0.5 && <Badge tone="ok">Σ{stoEdge.toFixed(1)}%</Badge>}
                      </div>
                    </td>
                  </motion.tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/* Positions                                                            */
/* ------------------------------------------------------------------ */

export function PositionsPanel({ positions }: { positions: PositionRow[] }) {
  const now = useTick(1000);
  return (
    <Card className="h-full">
      <CardHeader
        title="Open Positions"
        subtitle="Marked to the live order book, not frozen at entry"
        icon={<Briefcase />}
        action={
          <Link href="/positions">
            <Button variant="ghost" size="xs">
              Manage <ArrowRight className="h-3 w-3" />
            </Button>
          </Link>
        }
      />
      {positions.length === 0 ? (
        <EmptyState
          icon={<Briefcase />}
          title="No open positions"
          desc="The bot isn't holding anything right now. New entries appear here the moment a signal fires."
        />
      ) : (
        <div className="space-y-2 px-3 pb-3 pt-2">
          {positions.map((p) => {
            const pnlPct = p.entry_price ? ((p.mark_price - p.entry_price) / p.entry_price) * 100 : 0;
            return (
              <div key={p.trade_id} className="rounded-lg border border-line bg-raised p-3">
                <div className="flex items-center justify-between">
                  <span className="flex items-center gap-2 text-xs font-medium text-ink">
                    <span className="font-mono">{p.asset}</span>
                    <Badge tone={p.side === "YES" ? "ok" : "danger"}>{p.side}</Badge>
                    <span className="text-[10px] text-ink-faint">{p.strategy}</span>
                  </span>
                  <PnlText value={p.unrealized_pnl_usd} />
                </div>
                <div className="mt-2 grid grid-cols-4 gap-2 text-center">
                  <Mini label="Entry" v={p.entry_price.toFixed(3)} />
                  <Mini label="Mark" v={p.mark_price.toFixed(3)} />
                  <Mini label="Size" v={fmtUsd(p.size_usd)} />
                  <Mini label="uPnL %" v={`${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(1)}%`} tone={pnlPct >= 0 ? "ok" : "danger"} />
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

function Mini({ label, v, tone }: { label: string; v: string; tone?: "ok" | "danger" }) {
  return (
    <div>
      <div className="text-[9px] uppercase tracking-wider text-ink-faint">{label}</div>
      <div className={cn("mt-0.5 font-mono text-[11px] tabular", tone === "ok" ? "text-ok" : tone === "danger" ? "text-danger" : "text-ink")}>
        {v}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Strategy                                                             */
/* ------------------------------------------------------------------ */

export function StrategyPanel({ ov }: { ov: Overview }) {
  const strategies = ov.strategy ?? [];
  const max = Math.max(1, ...strategies.map((s) => Math.abs(s.pnl_usd)));
  return (
    <Card className="h-full">
      <CardHeader title="Strategy Performance" subtitle="Where the PnL is coming from" icon={<TrendingUp />} />
      {strategies.length === 0 ? (
        <EmptyState icon={<TrendingUp />} title="No closed trades yet" desc="Settlements will populate strategy stats." />
      ) : (
        <div className="space-y-3 px-4 pb-4 pt-3">
          {strategies.map((s) => (
            <div key={s.strategy}>
              <div className="mb-1 flex items-center justify-between text-[11px]">
                <span className="flex items-center gap-1.5 font-medium text-ink">
                  {s.strategy === "sum_to_one" ? "Sum-to-one" : "Latency arb"}
                  <span className="text-ink-faint">· {s.trades} trades</span>
                </span>
                <span className={cn("font-mono tabular", s.pnl_usd >= 0 ? "text-ok" : "text-danger")}>
                  {fmtUsd(s.pnl_usd, { sign: true })}
                </span>
              </div>
              <div className="flex h-2 gap-0.5 overflow-hidden rounded-full bg-raised">
                <div
                  className="bg-ok transition-all duration-500"
                  style={{ width: `${(Math.max(0, s.pnl_usd) / max) * 50}%` }}
                />
                <div
                  className="bg-danger transition-all duration-500"
                  style={{ width: `${(Math.max(0, -s.pnl_usd) / max) * 50}%` }}
                />
              </div>
              <div className="mt-1 text-[10px] text-ink-faint">
                win rate {fmtPct(s.win_rate_pct, 0)} · avg {fmtUsd(s.avg_pnl_usd, { sign: true })}
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/* Latency verdict                                                      */
/* ------------------------------------------------------------------ */

export function LatencyPanel({ ov }: { ov: Overview }) {
  const l = ov.latency;
  const verdict = l.verdict ?? "n/a";
  const verdictTone =
    verdict === "comfortable" ? ("ok" as const) : verdict === "tight" ? ("warn" as const) : ("danger" as const);
  const windowMs = (l.window_s ?? 2.0) * 1000;

  return (
    <Card className="h-full">
      <CardHeader
        title="Timing Budget"
        subtitle={`p95 response vs the ${windowMs.toFixed(0)}ms arbitrage window`}
        icon={<Timer />}
        action={<Badge tone={verdictTone}>{verdict}</Badge>}
      />
      <div className="px-4 pb-4 pt-3">
        <div className="mb-3 grid grid-cols-2 gap-2">
          <div className="rounded-lg border border-line bg-raised p-2.5 text-center">
            <div className="text-[9px] uppercase tracking-wider text-ink-faint">tick → order p50</div>
            <div className="mt-0.5 font-mono text-base tabular text-ink">
              {l.tick_to_order_p50_ms !== null && l.tick_to_order_p50_ms !== undefined
                ? `${Math.round(l.tick_to_order_p50_ms)}ms`
                : "—"}
            </div>
          </div>
          <div className="rounded-lg border border-line bg-raised p-2.5 text-center">
            <div className="text-[9px] uppercase tracking-wider text-ink-faint">tick → order p95</div>
            <div className="mt-0.5 font-mono text-base tabular text-ink">
              {l.tick_to_order_p95_ms !== null && l.tick_to_order_p95_ms !== undefined
                ? `${Math.round(l.tick_to_order_p95_ms)}ms`
                : "—"}
            </div>
          </div>
        </div>
        <LatencyBudget
          tickToSignalP95={l.tick_to_signal_p95_ms}
          signalToOrderP95={l.signal_to_order_p95_ms ?? undefined}
          platformDelayMs={l.platform_delay_ms ?? undefined}
          windowMs={windowMs}
        />
        <div className="mt-3 flex items-center justify-between text-[10px] text-ink-faint">
          <span>{l.cycles ?? 0} measured cycles</span>
          <span>{l.fired ?? 0} fired</span>
        </div>
      </div>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/* Activity                                                             */
/* ------------------------------------------------------------------ */

export function ActivityFeed({ limit = 8 }: { limit?: number }) {
  const { data, isLoading } = useActivity();
  const items = (data?.items ?? []).slice(0, limit);

  return (
    <Card className="h-full">
      <CardHeader
        title="Recent Activity"
        subtitle="The bot's decision trail"
        icon={<Activity />}
        action={
          <Link href="/activity">
            <Button variant="ghost" size="xs">
              Timeline <ArrowRight className="h-3 w-3" />
            </Button>
          </Link>
        }
      />
      <div className="px-4 pb-4 pt-2">
        {isLoading ? (
          <div className="space-y-2.5">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-10" />
            ))}
          </div>
        ) : items.length === 0 ? (
          <EmptyState icon={<Activity />} title="Nothing yet" desc="Signals, fills and settlements will stream in here." />
        ) : (
          <div className="relative space-y-0">
            {items.map((item, i) => (
              <div key={`${item.type}-${item.ts}-${i}`} className="relative flex gap-3 pb-3.5 last:pb-0">
                {i < items.length - 1 && (
                  <span className="absolute left-[5px] top-4 h-full w-px bg-line" />
                )}
                <ActivityDot type={item.type} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-xs font-medium text-ink">{item.label}</span>
                    {item.type === "trade" && (
                      <PnlText value={item.pnl_usd ?? 0} />
                    )}
                    {item.type === "signal" && (
                      <span className="font-mono text-[11px] tabular text-brand">
                        {item.edge_pct !== null && item.edge_pct !== undefined
                          ? `+${(item.edge_pct * 100).toFixed(1)}% edge`
                          : ""}
                      </span>
                    )}
                  </div>
                  <div className="mt-0.5 flex items-center gap-1.5 text-[10px] text-ink-faint">
                    <span>{fmtTime(item.ts)}</span>
                    {item.strategy && <span>· {item.strategy}</span>}
                    {item.exit_reason && <span>· {item.exit_reason}</span>}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </Card>
  );
}

export function ActivityDot({ type }: { type: string }) {
  const styles: Record<string, string> = {
    trade: "bg-brand",
    signal: "bg-info",
    risk: "bg-danger",
  };
  return (
    <span className={cn("mt-1 h-2.5 w-2.5 shrink-0 rounded-full border-2 border-surface", styles[type] ?? "bg-ink-faint")} />
  );
}

/* ------------------------------------------------------------------ */
/* Page-level loading & empty helpers                                   */
/* ------------------------------------------------------------------ */

export function OverviewSkeleton() {
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        {Array.from({ length: 6 }).map((_, i) => (
          <Skeleton key={i} className="h-[104px]" />
        ))}
      </div>
      <div className="grid gap-4 lg:grid-cols-3">
        <Skeleton className="h-[320px] lg:col-span-2" />
        <Skeleton className="h-[320px]" />
      </div>
    </div>
  );
}
