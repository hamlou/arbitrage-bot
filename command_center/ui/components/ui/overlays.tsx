"use client";

import * as React from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Check, ChevronDown, X } from "lucide-react";
import { cn } from "@/lib/utils";

/* ------------------------------------------------------------------ */
/* Drawer (right-side panel, used for trade details)                   */
/* ------------------------------------------------------------------ */

export function Drawer({
  open,
  onClose,
  title,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title?: React.ReactNode;
  children: React.ReactNode;
}) {
  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    if (open) window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            className="fixed inset-0 z-40 bg-black/50 backdrop-blur-[2px]"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={onClose}
          />
          <motion.div
            className="fixed right-0 top-0 z-50 flex h-full w-full max-w-[480px] flex-col border-l border-line bg-surface shadow-pop"
            initial={{ x: 40, opacity: 0 }}
            animate={{ x: 0, opacity: 1 }}
            exit={{ x: 40, opacity: 0 }}
            transition={{ type: "spring", damping: 30, stiffness: 350 }}
          >
            <div className="flex items-center justify-between border-b border-line px-5 py-4">
              <div className="min-w-0 pr-3 text-sm font-semibold">{title}</div>
              <button
                onClick={onClose}
                className="rounded-md p-1 text-ink-faint transition-colors hover:bg-raised hover:text-ink"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto px-5 py-4">{children}</div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}

/* ------------------------------------------------------------------ */
/* Dropdown menu                                                        */
/* ------------------------------------------------------------------ */

export function Dropdown({
  trigger,
  children,
  align = "right",
  className,
}: {
  trigger: React.ReactNode;
  children: React.ReactNode | ((close: () => void) => React.ReactNode);
  align?: "left" | "right";
  className?: string;
}) {
  const [open, setOpen] = React.useState(false);
  const ref = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    if (open) document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <div onClick={() => setOpen((v) => !v)}>{trigger}</div>
      <AnimatePresence>
        {open && (
          <motion.div
            className={cn(
              "absolute z-40 mt-2 min-w-[180px] overflow-hidden rounded-lg border border-line bg-elevated p-1 shadow-pop",
              align === "right" ? "right-0" : "left-0",
              className
            )}
            initial={{ opacity: 0, y: -4, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -4, scale: 0.98 }}
            transition={{ duration: 0.15 }}
          >
            {typeof children === "function" ? children(() => setOpen(false)) : children}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

export function MenuItem({
  onClick,
  children,
  active,
  danger,
}: {
  onClick?: () => void;
  children: React.ReactNode;
  active?: boolean;
  danger?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-[13px] transition-colors",
        active ? "bg-brand-soft text-brand" : danger ? "text-danger hover:bg-danger-soft" : "text-ink hover:bg-raised"
      )}
    >
      {children}
    </button>
  );
}

/* ------------------------------------------------------------------ */
/* Tabs                                                                */
/* ------------------------------------------------------------------ */

export function Tabs({
  tabs,
  value,
  onChange,
}: {
  tabs: { key: string; label: React.ReactNode; count?: number }[];
  value: string;
  onChange: (key: string) => void;
}) {
  return (
    <div className="inline-flex items-center gap-0.5 rounded-lg border border-line bg-raised p-0.5">
      {tabs.map((t) => (
        <button
          key={t.key}
          onClick={() => onChange(t.key)}
          className={cn(
            "flex h-7 items-center gap-1.5 rounded-md px-2.5 text-xs font-medium transition-all",
            value === t.key
              ? "bg-elevated text-ink shadow-[0_1px_2px_rgb(0_0_0/0.3)]"
              : "text-ink-muted hover:text-ink"
          )}
        >
          {t.label}
          {t.count !== undefined && (
            <span
              className={cn(
                "rounded-full px-1.5 font-mono text-[10px] tabular",
                value === t.key ? "bg-brand-soft text-brand" : "bg-raised text-ink-faint"
              )}
            >
              {t.count}
            </span>
          )}
        </button>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Select (styled native — zero flakiness)                             */
/* ------------------------------------------------------------------ */

export function Select({
  value,
  onChange,
  options,
  className,
}: {
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
  className?: string;
}) {
  return (
    <div className={cn("relative inline-flex", className)}>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={cn(
          "h-8 w-full cursor-pointer appearance-none rounded-lg border border-line bg-raised pl-3 pr-8 text-xs text-ink",
          "focus:border-brand/50 focus:outline-none focus:ring-2 focus:ring-brand/20"
        )}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value} className="bg-elevated">
            {o.label}
          </option>
        ))}
      </select>
      <ChevronDown className="pointer-events-none absolute right-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-ink-faint" />
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Switch                                                              */
/* ------------------------------------------------------------------ */

export function Switch({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label?: string;
}) {
  return (
    <button
      onClick={() => onChange(!checked)}
      className="group inline-flex items-center gap-2"
      aria-pressed={checked}
    >
      <span
        className={cn(
          "relative h-4.5 w-8 rounded-full border transition-colors duration-200",
          checked ? "border-brand/40 bg-brand" : "border-line bg-raised"
        )}
        style={{ height: 18, width: 32 }}
      >
        <span
          className={cn(
            "absolute top-1/2 -translate-y-1/2 rounded-full bg-white shadow transition-all duration-200",
            checked ? "left-[15px]" : "left-[3px]"
          )}
          style={{ width: 12, height: 12 }}
        />
      </span>
      {label && <span className="text-xs text-ink-muted group-hover:text-ink">{label}</span>}
    </button>
  );
}

/* ------------------------------------------------------------------ */
/* Tooltip (CSS hover)                                                 */
/* ------------------------------------------------------------------ */

export function Tip({ label, children }: { label: React.ReactNode; children: React.ReactNode }) {
  return (
    <span className="group/ti relative inline-flex">
      {children}
      <span className="pointer-events-none absolute bottom-full left-1/2 z-50 mb-1.5 -translate-x-1/2 whitespace-nowrap rounded-md border border-line bg-raised px-2 py-1 text-[11px] text-ink opacity-0 shadow-pop transition-opacity duration-150 group-hover/ti:opacity-100">
        {label}
      </span>
    </span>
  );
}

export function MenuCheck({ checked }: { checked: boolean }) {
  return <span className="w-4">{checked && <Check className="h-3.5 w-3.5 text-brand" />}</span>;
}
