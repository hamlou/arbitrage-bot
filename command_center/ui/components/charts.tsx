"use client";

import * as React from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { EquityPoint } from "@/lib/types";
import { fmtUsd } from "@/lib/format";

/* ------------------------------------------------------------------ */
/* EquityChart — the profit timeline                                   */
/* ------------------------------------------------------------------ */

type EquitySeriesPoint = { ts: number; equity: number };

export function EquityChart({ points }: { points: EquityPoint[] }) {
  const data: EquitySeriesPoint[] = React.useMemo(
    () =>
      points.map((p) => ({
        ts: p.ts * 1000,
        equity: Math.round(p.balance_usd * 100) / 100,
      })),
    [points]
  );

  if (data.length < 2) {
    return (
      <div className="flex h-full min-h-[220px] items-center justify-center text-xs text-ink-faint">
        Collecting equity history… run the bot to see the curve.
      </div>
    );
  }

  const first = data[0].equity;
  const lastPoint = data[data.length - 1];
  const last = lastPoint.equity;
  const up = last >= first;
  const startLabel = new Date(data[0].ts).toLocaleTimeString("en-US", { hour12: false });
  const endLabel = new Date(lastPoint.ts).toLocaleTimeString("en-US", { hour12: false });

  return (
    <div className="relative">
      <div className="pointer-events-none absolute left-4 top-3 z-10 flex items-center gap-3 font-mono text-[11px] tabular">
        <span className={up ? "text-ok" : "text-danger"}>
          {up ? "▲" : "▼"} {fmtUsd(Math.abs(last - first))}
        </span>
        <span className="text-ink-faint">
          {data.length} pts · {startLabel} → {endLabel}
        </span>
      </div>
      <div className="h-[240px] w-full pt-6">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
            <defs>
              <linearGradient id="eqFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="var(--brand)" stopOpacity={0.32} />
                <stop offset="100%" stopColor="var(--brand)" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke="var(--line)" strokeDasharray="3 3" vertical={false} />
            <XAxis
              dataKey="ts"
              tickFormatter={(v: number) => new Date(v).toLocaleTimeString("en-US", { hour12: false })}
              tick={{ fill: "var(--ink-faint)", fontSize: 10, fontFamily: "var(--font-mono)" }}
              axisLine={false}
              tickLine={false}
              minTickGap={80}
            />
            <YAxis
              domain={["auto", "auto"]}
              tick={{ fill: "var(--ink-faint)", fontSize: 10, fontFamily: "monospace" }}
              axisLine={false}
              tickLine={false}
              width={54}
              tickFormatter={(v: number) => fmtUsd(v)}
            />
            <Tooltip
              contentStyle={{
                background: "var(--elevated)",
                border: "1px solid var(--line)",
                borderRadius: 10,
                fontSize: 12,
                color: "var(--ink)",
                boxShadow: "0 12px 40px -8px rgb(0 0 0 / 0.5)",
              }}
              labelFormatter={(label) => new Date(Number(label)).toLocaleString("en-US", { hour12: false })}
              formatter={(value) => [fmtUsd(Number(value)), "Equity"]}
            />
            <Area
              type="monotone"
              dataKey="equity"
              stroke="var(--brand)"
              strokeWidth={2}
              fill="url(#eqFill)"
              isAnimationActive={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* LatencyBudget — the timing budget breakdown vs the arbitrage window */
/* ------------------------------------------------------------------ */

export function LatencyBudget({
  tickToSignalP95,
  signalToOrderP95,
  platformDelayMs,
  windowMs,
}: {
  tickToSignalP95: number | null | undefined;
  signalToOrderP95: number | null | undefined;
  platformDelayMs: number | null | undefined;
  windowMs: number | null | undefined;
}) {
  const w = windowMs ?? 2000;
  const segs = [
    { label: "tick → signal", value: tickToSignalP95 ?? 0, color: "var(--info)" },
    { label: "signal → order", value: signalToOrderP95 ?? 0, color: "var(--brand)" },
    { label: "platform delay", value: platformDelayMs ?? 0, color: "var(--warn)" },
  ];
  const total = segs.reduce((a, s) => a + s.value, 0);
  const headroom = Math.max(0, w - total);
  const totalPct = Math.min(100, (total / w) * 100);

  return (
    <div>
      <div className="relative mt-2 h-3 overflow-hidden rounded-full bg-raised">
        <div className="absolute inset-y-0 left-0 flex" style={{ width: `${totalPct}%` }}>
          {segs.map((s) => {
            const wPct = s.value > 0 ? Math.max(2, (s.value / w) * 100) : 0;
            return <div key={s.label} style={{ width: `${wPct}%`, background: s.color }} className="h-full" />;
          })}
        </div>
        <div
          className="absolute inset-y-0 w-px bg-danger"
          style={{ left: "100%" }}
        />
      </div>
      <div className="mt-1.5 flex justify-between font-mono text-[10px] text-ink-faint tabular">
        <span>0ms</span>
        <span>window {Math.round(w)}ms</span>
      </div>
      <div className="mt-3 space-y-1.5">
        {segs.map((s) => (
          <div key={s.label} className="flex items-center justify-between text-[11px]">
            <span className="flex items-center gap-1.5 text-ink-muted">
              <span className="h-2 w-2 rounded-[3px]" style={{ background: s.color }} />
              {s.label}
            </span>
            <span className="font-mono tabular text-ink">{Math.round(s.value)}ms</span>
          </div>
        ))}
        <div className="flex items-center justify-between border-t border-line pt-1.5 text-[11px]">
          <span className="flex items-center gap-1.5 text-ok">
            <span className="h-2 w-2 rounded-[3px] bg-ok" />
            headroom
          </span>
          <span className="font-mono font-medium tabular text-ok">{Math.round(headroom)}ms</span>
        </div>
      </div>
    </div>
  );
}
