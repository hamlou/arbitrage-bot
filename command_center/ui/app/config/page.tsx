"use client";

import { useConfig, useRiskEvents } from "@/lib/api";
import { fmtDateTime } from "@/lib/format";
import { Card, CardHeader, Skeleton } from "@/components/ui/primitives";
import { SectionTitle, EmptyState } from "@/components/widgets";
import { ShieldAlert, SlidersHorizontal } from "lucide-react";

interface ConfigGroup {
  title: string;
  desc: string;
  keys: string[];
}

const GROUPS: ConfigGroup[] = [
  {
    title: "Risk Guardrails",
    desc: "Hard limits that stop the bot from destroying the account",
    keys: [
      "MAX_POSITION_PCT",
      "MAX_TOTAL_EXPOSURE_PCT",
      "DAILY_LOSS_HALT_PCT",
      "TOTAL_DRAWDOWN_KILL_PCT",
    ],
  },
  {
    title: "Entry Discipline",
    desc: "The fixes that stopped the $100 bleed — price caps and fee-aware edges",
    keys: [
      "MAX_DIRECTIONAL_ENTRY_PRICE",
      "EDGE_THRESHOLD_PCT",
      "MIN_CONFIDENCE",
      "TAKER_FEE_PCT",
      "CROSS_EXCHANGE_TOLERANCE_PCT",
      "MIN_MARKET_LIQUIDITY_USD",
    ],
  },
  {
    title: "Position Sizing & Fills",
    desc: "How the bot sizes and simulates real fills",
    keys: ["SIMULATED_FILL_LATENCY_S", "MIN_ORDER_SIZE_USD", "TICK_SIZE", "STARTING_PAPER_BALANCE_USD"],
  },
  {
    title: "Exit Logic",
    desc: "When positions are closed before expiry",
    keys: ["TAKE_PROFIT_PCT", "EDGE_REVERSAL_EXIT_THRESHOLD_PCT"],
  },
  {
    title: "Sum-to-One Arbitrage",
    desc: "Risk-free YES+NO lock-in strategy",
    keys: ["SUM_TO_ONE_MIN_EDGE_PCT", "SUM_TO_ONE_MAX_POSITION_PCT"],
  },
  {
    title: "Timing Budget",
    desc: "The arbitrage window math",
    keys: ["ASSUMED_ARBITRAGE_WINDOW_S", "PLATFORM_TAKER_DELAY_MS"],
  },
  {
    title: "System",
    desc: "Discovery cadence and Telegram digest",
    keys: ["MARKET_DISCOVERY_INTERVAL_S", "TELEGRAM_STATUS_INTERVAL_HOURS", "PAPER_MODE"],
  },
];

const HUMAN: Record<string, string> = {
  MAX_POSITION_PCT: "Per-trade size cap",
  MAX_TOTAL_EXPOSURE_PCT: "Total exposure cap",
  DAILY_LOSS_HALT_PCT: "Daily loss halt",
  TOTAL_DRAWDOWN_KILL_PCT: "Kill switch drawdown",
  MAX_DIRECTIONAL_ENTRY_PRICE: "Max entry price",
  EDGE_THRESHOLD_PCT: "Min edge to fire",
  MIN_CONFIDENCE: "Min confidence",
  TAKER_FEE_PCT: "Taker fee (fee-aware gate)",
  CROSS_EXCHANGE_TOLERANCE_PCT: "Binance↔Coinbase tolerance",
  MIN_MARKET_LIQUIDITY_USD: "Min market liquidity",
  SIMULATED_FILL_LATENCY_S: "Simulated fill latency",
  MIN_ORDER_SIZE_USD: "Min order size",
  TICK_SIZE: "Tick size",
  STARTING_PAPER_BALANCE_USD: "Paper starting balance",
  TAKE_PROFIT_PCT: "Take-profit threshold",
  EDGE_REVERSAL_EXIT_THRESHOLD_PCT: "Edge reversal exit",
  SUM_TO_ONE_MIN_EDGE_PCT: "Min lock-in edge",
  SUM_TO_ONE_MAX_POSITION_PCT: "Max sum-to-one size",
  ASSUMED_ARBITRAGE_WINDOW_S: "Assumed window",
  PLATFORM_TAKER_DELAY_MS: "Platform taker delay",
  MARKET_DISCOVERY_INTERVAL_S: "Discovery interval",
  TELEGRAM_STATUS_INTERVAL_HOURS: "Telegram digest interval",
  PAPER_MODE: "Paper mode",
};

export default function ConfigPage() {
  const { data, isLoading } = useConfig();
  const { data: risk } = useRiskEvents();
  const cfg = data?.config ?? {};

  const fmt = (key: string, v: unknown): string => {
    if (typeof v === "number") {
      // CROSS_EXCHANGE_TOLERANCE_PCT is already in percent units (0.1 = 0.1%),
      // unlike every other *_PCT which is a fraction — special-case it.
      if (key === "CROSS_EXCHANGE_TOLERANCE_PCT") return `${v}%`;
      if (key.endsWith("_PCT")) return `${(v * 100).toFixed(1)}%`;
      if (key.endsWith("_USD") || key.endsWith("_PRICE")) return `$${v.toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
      if (key.endsWith("_S")) return `${v}s`;
      if (key.endsWith("_MS")) return `${v}ms`;
      if (key.endsWith("_HOURS")) return `${v}h`;
      return v.toFixed(2);
    }
    return String(v);
  };

  return (
    <div className="space-y-4">
      <SectionTitle
        eyebrow="System"
        title="Risk & Configuration"
        desc="Every threshold that governs the bot — read-only view. Changes are made in config/settings.py, never from here."
      />

      <div className="grid gap-4 md:grid-cols-2">
        {GROUPS.map((g) => (
          <Card key={g.title}>
            <CardHeader title={g.title} subtitle={g.desc} icon={<SlidersHorizontal className="h-3.5 w-3.5" />} />
            <div className="px-4 pb-4 pt-2">
              {isLoading ? (
                <div className="space-y-2">
                  {g.keys.map((k) => (
                    <Skeleton key={k} className="h-8" />
                  ))}
                </div>
              ) : (
                <div className="space-y-1.5">
                  {g.keys.map((k) => (
                    <div key={k} className="flex items-center justify-between rounded-lg border border-line bg-raised px-3 py-2">
                      <div>
                        <div className="text-[11px] font-medium text-ink">{HUMAN[k] ?? k.replace(/_/g, " ").toLowerCase()}</div>
                        <div className="font-mono text-[9.5px] text-ink-faint">{k}</div>
                      </div>
                      <span className="font-mono text-xs font-semibold tabular text-brand">{fmt(k, cfg[k])}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </Card>
        ))}
      </div>

      <Card>
        <CardHeader
          title="Risk Events"
          subtitle="Halts, kill-switch trips and other safety activations"
          icon={<ShieldAlert className="h-3.5 w-3.5" />}
        />
        <div className="px-4 pb-4 pt-1">
          {(risk?.events ?? []).length === 0 ? (
            <EmptyState
              icon={<ShieldAlert />}
              title="No risk events"
              desc="The safety systems have never had to activate. That's the good outcome."
            />
          ) : (
            <div className="space-y-1.5">
              {(risk?.events ?? []).map((e, i) => {
                const ev = e as Record<string, unknown>;
                return (
                  <div key={i} className="flex items-center justify-between rounded-lg border border-line bg-raised px-3 py-2 text-[11px]">
                    <span className="font-medium text-danger">{String(ev.event_type)}</span>
                    <span className="text-ink-muted">{String(ev.detail ?? "")}</span>
                    <span className="font-mono text-ink-faint tabular">{fmtDateTime(ev.ts as number)}</span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </Card>
    </div>
  );
}
