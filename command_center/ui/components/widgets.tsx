"use client";

import * as React from "react";
import { Area, AreaChart, ResponsiveContainer } from "recharts";
import { cn } from "@/lib/utils";
import { fmtUsd, pnlColor } from "@/lib/format";

/* ------------------------------------------------------------------ */
/* useTick — re-renders every second (live countdowns)                 */
/* ------------------------------------------------------------------ */

export function useTick(intervalMs = 1000): number {
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}

/* ------------------------------------------------------------------ */
/* CountUp — animates numeric changes                                  */
/* ------------------------------------------------------------------ */

export function CountUp({
  value,
  format = (v) => v.toFixed(2),
  className,
  duration = 600,
}: {
  value: number;
  format?: (v: number) => string;
  className?: string;
  duration?: number;
}) {
  const [display, setDisplay] = React.useState(value);
  const prevRef = React.useRef(value);

  React.useEffect(() => {
    const from = prevRef.current;
    const to = value;
    if (from === to) return;
    prevRef.current = to;
    let raf = 0;
    const start = performance.now();
    const tick = (t: number) => {
      const p = Math.min(1, (t - start) / duration);
      const eased = 1 - Math.pow(1 - p, 3);
      setDisplay(from + (to - from) * eased);
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value, duration]);

  return <span className={cn("tabular", className)}>{format(display)}</span>;
}

/* ------------------------------------------------------------------ */
/* Sparkline — tiny trend line for stat cards                          */
/* ------------------------------------------------------------------ */

export function Sparkline({
  data,
  color = "var(--brand)",
  height = 32,
  id,
}: {
  data: number[];
  color?: string;
  height?: number;
  id: string;
}) {
  const points = data.map((v, i) => ({ i, v }));
  if (points.length < 2) {
    return <div className="flex items-center text-[10px] text-ink-faint">no history yet</div>;
  }
  const gradId = `spark-${id}`;
  return (
    <div style={{ height }} className="w-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={points} margin={{ top: 2, right: 0, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.35} />
              <stop offset="100%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <Area
            type="monotone"
            dataKey="v"
            stroke={color}
            strokeWidth={1.5}
            fill={`url(#${gradId})`}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* StatCard — the KPI cell                                             */
/* ------------------------------------------------------------------ */

export function StatCard({
  label,
  value,
  display,
  delta,
  deltaTone,
  icon,
  spark,
  sparkColor,
  footer,
  onClick,
  className,
}: {
  label: string;
  value: number | null | undefined;
  display?: string | ((v: number) => string);
  delta?: number | null;
  deltaTone?: "ok" | "danger" | "neutral";
  icon?: React.ReactNode;
  spark?: number[];
  sparkColor?: string;
  footer?: React.ReactNode;
  onClick?: () => void;
  className?: string;
}) {
  const tone =
    deltaTone ??
    (delta === null || delta === undefined || delta === 0
      ? "neutral"
      : delta > 0
        ? "ok"
        : "danger");
  return (
    <div
      onClick={onClick}
      className={cn(
        "group relative overflow-hidden rounded-lg border border-line bg-surface p-4 shadow-card transition-all duration-200",
        onClick && "cursor-pointer hover:border-edge hover:bg-elevated",
        className
      )}
    >
      <div className="flex items-center justify-between">
        <span className="whitespace-nowrap text-[11px] font-medium uppercase tracking-[0.08em] text-ink-faint">
          {label}
        </span>
        {icon && <span className="text-ink-faint [&>svg]:h-3.5 [&>svg]:w-3.5">{icon}</span>}
      </div>
      <div className="mt-1.5 flex items-baseline gap-2">
        <CountUp
          value={value ?? 0}
          format={typeof display === "function" ? display : (v) => display ?? v.toFixed(2)}
          className="text-[22px] font-semibold tracking-tight text-ink"
        />
        {delta !== undefined && delta !== null && (
          <span
            className={cn(
              "text-[11px] font-medium tabular",
              tone === "ok" ? "text-ok" : tone === "danger" ? "text-danger" : "text-ink-faint"
            )}
          >
            {delta > 0 ? "▲" : delta < 0 ? "▼" : ""} {Math.abs(delta).toFixed(2)}
          </span>
        )}
      </div>
      {footer && <div className="mt-0.5 text-[11px] text-ink-faint">{footer}</div>}
      {spark && spark.length > 1 && (
        <div className="pointer-events-none absolute inset-x-0 bottom-0 opacity-80">
          <Sparkline data={spark} id={label.replace(/\W+/g, "")} color={sparkColor} height={26} />
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Status pills / dots                                                 */
/* ------------------------------------------------------------------ */

export function LiveDot({ tone = "ok", pulse }: { tone?: "ok" | "warn" | "danger" | "muted"; pulse?: boolean }) {
  const color =
    tone === "ok"
      ? "bg-ok"
      : tone === "warn"
        ? "bg-warn"
        : tone === "danger"
          ? "bg-danger"
          : "bg-ink-faint";
  return (
    <span className="relative inline-flex h-1.5 w-1.5">
      {pulse && <span className={cn("absolute inline-flex h-full w-full animate-ping rounded-full opacity-60", color)} />}
      <span className={cn("relative inline-flex h-1.5 w-1.5 rounded-full", color)} />
    </span>
  );
}

export function StatusPill({
  healthy,
  label,
  detail,
}: {
  healthy: boolean | null;
  label: string;
  detail?: string;
}) {
  const tone = healthy === null ? "muted" : healthy ? "ok" : "danger";
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-line bg-raised px-2.5 py-1 text-[11px] text-ink-muted">
      <LiveDot tone={tone} pulse={healthy === true} />
      <span className="font-medium text-ink">{label}</span>
      {detail && <span className="text-ink-faint">{detail}</span>}
    </span>
  );
}

/* ------------------------------------------------------------------ */
/* SectionTitle                                                         */
/* ------------------------------------------------------------------ */

export function SectionTitle({
  eyebrow,
  title,
  desc,
  right,
  className,
}: {
  eyebrow?: string;
  title: React.ReactNode;
  desc?: string;
  right?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("mb-5 flex items-end justify-between gap-4", className)}>
      <div>
        {eyebrow && (
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-[0.14em] text-brand">
            {eyebrow}
          </div>
        )}
        <h1 className="text-xl font-semibold tracking-tight text-ink">{title}</h1>
        {desc && <p className="mt-1 max-w-2xl text-[13px] text-ink-muted">{desc}</p>}
      </div>
      {right && <div className="flex shrink-0 items-center gap-2">{right}</div>}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* EmptyState                                                           */
/* ------------------------------------------------------------------ */

export function EmptyState({
  icon,
  title,
  desc,
}: {
  icon?: React.ReactNode;
  title: string;
  desc?: string;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-10 text-center">
      {icon && <div className="text-ink-faint [&>svg]:h-6 [&>svg]:w-6">{icon}</div>}
      <div className="text-sm font-medium text-ink-muted">{title}</div>
      {desc && <div className="max-w-xs text-xs text-ink-faint">{desc}</div>}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* PnlText — colored, sign-aware money                                  */
/* ------------------------------------------------------------------ */

export function PnlText({ value, decimals }: { value: number | null | undefined; decimals?: number }) {
  return (
    <span className={cn("font-mono text-[13px] font-medium tabular", pnlColor(value))}>
      {fmtUsd(value, { sign: true, decimals })}
    </span>
  );
}
