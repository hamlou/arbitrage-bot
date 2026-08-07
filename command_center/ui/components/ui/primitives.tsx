"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

/* ------------------------------------------------------------------ */
/* Button                                                              */
/* ------------------------------------------------------------------ */

type ButtonVariant = "primary" | "secondary" | "ghost" | "outline" | "danger";
type ButtonSize = "xs" | "sm" | "md";

const btnVariants: Record<ButtonVariant, string> = {
  primary:
    "bg-brand text-white hover:bg-brand/90 active:scale-[0.98] transition-all shadow-[0_1px_0_rgb(255_255_255/0.14)_inset,0_4px_16px_-6px_rgb(var(--brand)/0.5)]",
  secondary:
    "bg-raised text-ink hover:bg-elevated border border-line active:scale-[0.98] transition-all",
  ghost: "text-ink-muted hover:text-ink hover:bg-raised transition-colors",
  outline: "border border-edge text-ink hover:bg-raised transition-colors",
  danger: "bg-danger/15 text-danger border border-danger/25 hover:bg-danger/25 transition-colors",
};

const btnSizes: Record<ButtonSize, string> = {
  xs: "h-6 px-2 text-[11px] rounded-[7px] gap-1",
  sm: "h-8 px-3 text-xs rounded-lg gap-1.5",
  md: "h-9 px-4 text-sm rounded-lg gap-2",
};

export function Button({
  variant = "primary",
  size = "md",
  className,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
}) {
  return (
    <button
      className={cn(
        "inline-flex items-center justify-center font-medium select-none",
        "disabled:opacity-45 disabled:pointer-events-none whitespace-nowrap",
        btnVariants[variant],
        btnSizes[size],
        className
      )}
      {...props}
    />
  );
}

/* ------------------------------------------------------------------ */
/* Badge                                                               */
/* ------------------------------------------------------------------ */

export type Tone = "neutral" | "ok" | "warn" | "danger" | "info" | "brand";

const toneClasses: Record<Tone, string> = {
  neutral: "bg-raised text-ink-muted border-line",
  ok: "bg-ok-soft text-ok border-ok/20",
  warn: "bg-warn-soft text-warn border-warn/20",
  danger: "bg-danger-soft text-danger border-danger/20",
  info: "bg-info-soft text-info border-info/20",
  brand: "bg-brand-soft text-brand border-brand/20",
};

export function Badge({
  tone = "neutral",
  dot,
  className,
  children,
}: {
  tone?: Tone;
  dot?: boolean;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium leading-4",
        toneClasses[tone],
        className
      )}
    >
      {dot && <span className="h-1.5 w-1.5 rounded-full bg-current" />}
      {children}
    </span>
  );
}

/* ------------------------------------------------------------------ */
/* Card                                                                */
/* ------------------------------------------------------------------ */

export function Card({
  className,
  hover,
  ...props
}: React.HTMLAttributes<HTMLDivElement> & { hover?: boolean }) {
  return (
    <div
      className={cn(
        "rounded-lg border border-line bg-surface shadow-card",
        hover &&
          "transition-all duration-200 hover:border-edge hover:bg-elevated hover:shadow-pop",
        className
      )}
      {...props}
    />
  );
}

export function CardHeader({
  title,
  subtitle,
  icon,
  action,
  className,
}: {
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  icon?: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex items-start justify-between gap-3 px-4 pt-4 pb-0", className)}>
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          {icon && <span className="text-ink-faint [&>svg]:h-3.5 [&>svg]:w-3.5">{icon}</span>}
          <h3 className="text-[13px] font-semibold tracking-tight text-ink">{title}</h3>
        </div>
        {subtitle && <p className="mt-0.5 text-[11px] text-ink-faint">{subtitle}</p>}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Skeleton / Spinner / Progress / Separator / Kbd / Input             */
/* ------------------------------------------------------------------ */

export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      className={cn(
        "rounded-md bg-elevated animate-shimmer bg-[linear-gradient(90deg,transparent,rgb(var(--ink-faint)/0.12),transparent)] bg-[length:400px_100%]",
        className
      )}
    />
  );
}

export function Spinner({ className }: { className?: string }) {
  return (
    <svg
      className={cn("h-4 w-4 animate-spin text-ink-faint", className)}
      viewBox="0 0 24 24"
      fill="none"
    >
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
      <path
        className="opacity-80"
        fill="currentColor"
        d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
      />
    </svg>
  );
}

export function Progress({
  value,
  tone = "brand",
  className,
}: {
  value: number;
  tone?: Tone;
  className?: string;
}) {
  const toneBg: Record<Tone, string> = {
    neutral: "bg-ink-muted",
    ok: "bg-ok",
    warn: "bg-warn",
    danger: "bg-danger",
    info: "bg-info",
    brand: "bg-brand",
  };
  const pct = Math.max(0, Math.min(100, value));
  return (
    <div className={cn("h-1.5 w-full overflow-hidden rounded-full bg-raised", className)}>
      <div
        className={cn("h-full rounded-full transition-all duration-500", toneBg[tone])}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

export function Separator({ className }: { className?: string }) {
  return <div className={cn("h-px w-full bg-line", className)} />;
}

export function Kbd({ children }: { children: React.ReactNode }) {
  return (
    <kbd className="inline-flex h-5 min-w-[20px] items-center justify-center rounded-[5px] border border-line bg-raised px-1.5 font-mono text-[10px] text-ink-muted">
      {children}
    </kbd>
  );
}

export function Input({
  className,
  ...props
}: React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={cn(
        "h-8 w-full rounded-lg border border-line bg-raised px-3 text-[13px] text-ink placeholder:text-ink-faint",
        "focus:border-brand/50 focus:outline-none focus:ring-2 focus:ring-brand/20 transition-all",
        className
      )}
      {...props}
    />
  );
}
