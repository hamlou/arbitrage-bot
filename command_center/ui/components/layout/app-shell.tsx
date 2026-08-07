"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { AnimatePresence, motion } from "framer-motion";
import {
  Activity,
  ArrowLeftRight,
  Bell,
  Briefcase,
  CandlestickChart,
  ChevronRight,
  Gauge,
  Menu,
  Moon,
  Search,
  Settings2,
  Sun,
  Timer,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Kbd } from "@/components/ui/primitives";
import { useOverview } from "@/lib/api";
import { useLiveWs, useWsStatus } from "@/lib/useLive";
import { LiveDot, StatusPill } from "@/components/widgets";

/* ------------------------------------------------------------------ */
/* navigation registry — also feeds the command palette                */
/* ------------------------------------------------------------------ */

export const NAV = [
  { href: "/", label: "Overview", icon: Gauge, group: "Command" },
  { href: "/trades", label: "Trades", icon: ArrowLeftRight, group: "Execution" },
  { href: "/positions", label: "Positions", icon: Briefcase, group: "Execution" },
  { href: "/markets", label: "Markets", icon: CandlestickChart, group: "Execution" },
  { href: "/latency", label: "Latency", icon: Timer, group: "Intelligence" },
  { href: "/activity", label: "Activity", icon: Activity, group: "Intelligence" },
  { href: "/config", label: "Risk & Config", icon: Settings2, group: "System" },
] as const;

const TITLES: Record<string, { title: string; sub: string }> = {
  "/": { title: "Command Center", sub: "Everything the bot knows, right now." },
  "/trades": { title: "Trade Ledger", sub: "Every fill, settlement and exit — drill into any trade." },
  "/positions": { title: "Open Positions", sub: "Live mark-to-market exposure across every market." },
  "/markets": { title: "Market Snapshot", sub: "Live windows Polymarket is running right now." },
  "/latency": { title: "Timing Analysis", sub: "Can the bot win the window? The evidence." },
  "/activity": { title: "Activity Stream", sub: "Signals, fills, settlements and risk events in one timeline." },
  "/config": { title: "Risk & Configuration", sub: "Every threshold that governs the bot's behavior." },
};

/* ------------------------------------------------------------------ */
/* theme                                                               */
/* ------------------------------------------------------------------ */

export function toggleTheme() {
  const root = document.documentElement;
  root.classList.toggle("light");
  try {
    localStorage.setItem("theme", root.classList.contains("light") ? "light" : "dark");
  } catch {
    /* ignore */
  }
}

export function isLight(): boolean {
  return typeof document !== "undefined" && document.documentElement.classList.contains("light");
}

/* ------------------------------------------------------------------ */
/* command palette                                                      */
/* ------------------------------------------------------------------ */

function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const router = useRouter();
  const [q, setQ] = React.useState("");
  const [sel, setSel] = React.useState(0);
  const inputRef = React.useRef<HTMLInputElement>(null);

  const items = React.useMemo(() => {
    const nav = NAV.map((n) => ({
      id: n.href,
      label: n.label,
      hint: "Go to page",
      group: n.group,
      run: () => router.push(n.href),
    }));
    return [
      ...nav,
      {
        id: "theme",
        label: isLight() ? "Switch to dark theme" : "Switch to light theme",
        hint: "Action",
        group: "Actions",
        run: toggleTheme,
      },
      {
        id: "export",
        label: "Export trades to CSV",
        hint: "Action",
        group: "Actions",
        run: () => {
          window.dispatchEvent(new CustomEvent("arb:export-csv"));
        },
      },
      {
        id: "docs",
        label: "API docs (FastAPI /docs)",
        hint: "Developer",
        group: "System",
        run: () => window.open("http://127.0.0.1:8787/docs", "_blank"),
      },
    ];
  }, [router]);

  const filtered = React.useMemo(() => {
    const s = q.trim().toLowerCase();
    if (!s) return items;
    return items.filter((i) => `${i.label} ${i.group}`.toLowerCase().includes(s));
  }, [q, items]);

  React.useEffect(() => {
    if (open) {
      setQ("");
      setSel(0);
      setTimeout(() => inputRef.current?.focus(), 30);
    }
  }, [open]);

  React.useEffect(() => {
    if (sel >= filtered.length) setSel(0);
  }, [filtered.length, sel]);

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-[60] flex items-start justify-center bg-black/60 px-4 pt-[12vh] backdrop-blur-sm"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onClick={onClose}
        >
          <motion.div
            className="w-full max-w-lg overflow-hidden rounded-xl border border-edge bg-elevated shadow-pop"
            initial={{ scale: 0.97, opacity: 0, y: -8 }}
            animate={{ scale: 1, opacity: 1, y: 0 }}
            exit={{ scale: 0.97, opacity: 0, y: -8 }}
            transition={{ duration: 0.15 }}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center gap-2.5 border-b border-line px-4">
              <Search className="h-4 w-4 text-ink-faint" />
              <input
                ref={inputRef}
                value={q}
                onChange={(e) => setQ(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "ArrowDown") {
                    e.preventDefault();
                    setSel((s) => Math.min(filtered.length - 1, s + 1));
                  } else if (e.key === "ArrowUp") {
                    e.preventDefault();
                    setSel((s) => Math.max(0, s - 1));
                  } else if (e.key === "Enter" && filtered[sel]) {
                    filtered[sel].run();
                    onClose();
                  }
                }}
                placeholder="Search pages, actions…"
                className="h-12 w-full bg-transparent text-sm text-ink placeholder:text-ink-faint focus:outline-none"
              />
              <Kbd>esc</Kbd>
            </div>
            <div className="max-h-[320px] overflow-y-auto p-1.5">
              {filtered.length === 0 && (
                <div className="px-3 py-6 text-center text-xs text-ink-faint">No matches for “{q}”</div>
              )}
              {filtered.map((item, i) => (
                <button
                  key={item.id}
                  onClick={() => {
                    item.run();
                    onClose();
                  }}
                  onMouseEnter={() => setSel(i)}
                  className={cn(
                    "flex w-full items-center justify-between rounded-lg px-3 py-2 text-left text-[13px]",
                    i === sel ? "bg-brand-soft text-brand" : "text-ink"
                  )}
                >
                  <span className="flex items-center gap-2.5">
                    <ChevronRight className={cn("h-3.5 w-3.5", i === sel ? "text-brand" : "text-ink-faint")} />
                    {item.label}
                  </span>
                  <span className="text-[10px] text-ink-faint">{item.hint}</span>
                </button>
              ))}
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

/* ------------------------------------------------------------------ */
/* notifications                                                        */
/* ------------------------------------------------------------------ */

function useAlerts() {
  const { data } = useOverview(5000);
  const alerts: { tone: "danger" | "warn" | "info"; text: string }[] = [];
  if (!data) return { alerts, unread: 0 };
  const a = data.account;
  const r = data.risk;
  if (a.kill_switch_tripped) alerts.push({ tone: "danger", text: "KILL SWITCH tripped — drawdown limit hit. No trading until reset." });
  if (a.daily_halted) alerts.push({ tone: "danger", text: "Daily loss halt active — trading stopped for the day." });
  if (a.paused) alerts.push({ tone: "warn", text: "Trading paused — no new positions until resumed." });
  if (!data.feeds.binance.healthy) alerts.push({ tone: "danger", text: "Binance feed unhealthy — all trading skipped." });
  if (!data.feeds.polymarket.healthy) alerts.push({ tone: "danger", text: "Polymarket feed unhealthy — all trading skipped." });
  if (data.feeds.binance.reconnects_10m > 10) alerts.push({ tone: "warn", text: `Binance reconnected ${data.feeds.binance.reconnects_10m}× in 10m — possible network trouble.` });
  if (data.feeds.polymarket.reconnects_10m > 10) alerts.push({ tone: "warn", text: `Polymarket reconnected ${data.feeds.polymarket.reconnects_10m}× in 10m — possible network trouble.` });
  if (a.alerts_muted) alerts.push({ tone: "info", text: "Telegram alerts muted — CRITICAL still delivered." });
  if (a.profit_factor !== null && a.profit_factor !== undefined && a.profit_factor < 1 && a.closed_trades > 3)
    alerts.push({ tone: "warn", text: "Profit factor below 1.0 — the book is losing more than it wins." });
  return { alerts, unread: alerts.filter((x) => x.tone !== "info").length };
}

/* ------------------------------------------------------------------ */
/* sidebar + topbar                                                     */
/* ------------------------------------------------------------------ */

function Sidebar({ mobile, onNavigate }: { mobile?: boolean; onNavigate?: () => void }) {
  const pathname = usePathname();
  const groups = ["Command", "Execution", "Intelligence", "System"];

  return (
    <div className="flex h-full w-[232px] flex-col border-r border-line bg-surface">
      <div className="flex h-14 items-center gap-2.5 border-b border-line px-5">
        <div className="flex h-7 w-7 items-center justify-center rounded-md bg-brand shadow-glow">
          <span className="font-mono text-[11px] font-bold text-white">A</span>
        </div>
        <div>
          <div className="text-[13px] font-semibold leading-none tracking-tight text-ink">Arb OS</div>
          <div className="mt-0.5 text-[9px] font-medium uppercase tracking-[0.16em] text-ink-faint">
            Command Center
          </div>
        </div>
      </div>

      <nav className="flex-1 space-y-5 overflow-y-auto px-3 py-4">
        {groups.map((g) => (
          <div key={g}>
            <div className="mb-1.5 px-2 text-[9.5px] font-semibold uppercase tracking-[0.14em] text-ink-faint">
              {g}
            </div>
            <div className="space-y-0.5">
              {NAV.filter((n) => n.group === g).map((n) => {
                const active = pathname === n.href;
                const Icon = n.icon;
                return (
                  <Link
                    key={n.href}
                    href={n.href}
                    onClick={onNavigate}
                    className={cn(
                      "group relative flex h-8 items-center gap-2.5 rounded-lg px-2.5 text-[13px] transition-all",
                      active ? "bg-brand-soft text-brand font-medium" : "text-ink-muted hover:bg-raised hover:text-ink"
                    )}
                  >
                    <Icon className={cn("h-3.5 w-3.5", active ? "text-brand" : "text-ink-faint group-hover:text-ink-muted")} />
                    {n.label}
                    {active && (
                      <motion.span
                        layoutId={mobile ? "nav-mobile" : "nav"}
                        className="absolute left-0 top-1/2 h-4 w-0.5 -translate-y-1/2 rounded-full bg-brand"
                      />
                    )}
                  </Link>
                );
              })}
            </div>
          </div>
        ))}
      </nav>

      <div className="border-t border-line p-3">
        <div className="rounded-lg border border-line bg-raised px-3 py-2.5">
          <div className="flex items-center justify-between">
            <span className="text-[10px] uppercase tracking-[0.12em] text-ink-faint">Engine</span>
            <span className="font-mono text-[10px] text-brand">v1.0</span>
          </div>
          <div className="mt-1 flex items-center gap-1.5 text-[11px] text-ink-muted">
            <LiveDot tone="ok" pulse />
            API connected
          </div>
        </div>
      </div>
    </div>
  );
}

function Topbar({ onMenu, onPalette }: { onMenu: () => void; onPalette: () => void }) {
  const pathname = usePathname();
  const meta = TITLES[pathname] ?? TITLES["/"];
  const { data: health } = useOverview(5000);
  const wsConnected = useWsStatus();
  const { alerts, unread } = useAlerts();
  const [notifOpen, setNotifOpen] = React.useState(false);
  const [light, setLight] = React.useState(false);

  React.useEffect(() => {
    setLight(isLight());
  }, []);

  const botOnline = health?.bot_online ?? false;

  return (
    <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-line bg-bg/90 px-4 backdrop-blur-xl lg:px-6">
      <button onClick={onMenu} className="rounded-md p-1.5 text-ink-muted hover:bg-raised lg:hidden">
        <Menu className="h-4 w-4" />
      </button>

      <div className="min-w-0">
        <div className="flex items-center gap-1.5 text-[10px] text-ink-faint">
          <span className="font-medium uppercase tracking-[0.12em] text-ink-faint">Arb OS</span>
          <ChevronRight className="h-2.5 w-2.5" />
          <span className="text-ink-muted">{meta.title}</span>
        </div>
        <div className="truncate text-[13px] font-semibold text-ink">{meta.sub}</div>
      </div>

      <div className="ml-auto flex items-center gap-2">
        <button
          onClick={onPalette}
          className="hidden h-8 items-center gap-2 rounded-lg border border-line bg-raised px-3 text-xs text-ink-faint transition-colors hover:border-edge hover:text-ink-muted sm:flex"
        >
          <Search className="h-3.5 w-3.5" />
          <span>Search…</span>
          <Kbd>⌘K</Kbd>
        </button>

        <StatusPill
          healthy={botOnline}
          label={botOnline ? "BOT LIVE" : "BOT OFFLINE"}
          detail={wsConnected ? "· stream" : undefined}
        />

        {/* notifications */}
        <div className="relative">
          <button
            onClick={() => setNotifOpen((v) => !v)}
            className="relative rounded-lg p-2 text-ink-muted transition-colors hover:bg-raised hover:text-ink"
          >
            <Bell className="h-4 w-4" />
            {unread > 0 && (
              <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-[16px] items-center justify-center rounded-full bg-danger px-1 font-mono text-[9px] font-bold text-white">
                {unread}
              </span>
            )}
          </button>
          <AnimatePresence>
            {notifOpen && (
              <motion.div
                className="absolute right-0 z-50 mt-1.5 w-80 overflow-hidden rounded-xl border border-edge bg-elevated shadow-pop"
                initial={{ opacity: 0, y: -4 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -4 }}
              >
                <div className="flex items-center justify-between border-b border-line px-3.5 py-2.5">
                  <span className="text-xs font-semibold text-ink">Notifications</span>
                  <span className="text-[10px] text-ink-faint">{alerts.length} active</span>
                </div>
                <div className="max-h-[300px] overflow-y-auto">
                  {alerts.length === 0 && (
                    <div className="px-3.5 py-6 text-center text-xs text-ink-faint">
                      All clear. Nothing needs attention.
                    </div>
                  )}
                  {alerts.map((a, i) => (
                    <div key={i} className="flex gap-2.5 border-b border-line/60 px-3.5 py-2.5">
                      <span
                        className={cn(
                          "mt-1 h-1.5 w-1.5 shrink-0 rounded-full",
                          a.tone === "danger" ? "bg-danger" : a.tone === "warn" ? "bg-warn" : "bg-info"
                        )}
                      />
                      <span className="text-xs leading-relaxed text-ink-muted">{a.text}</span>
                    </div>
                  ))}
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>

        <button
          onClick={() => {
            toggleTheme();
            setLight((v) => !v);
          }}
          className="rounded-lg p-2 text-ink-muted transition-colors hover:bg-raised hover:text-ink"
          aria-label="Toggle theme"
        >
          {light ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
        </button>
      </div>
    </header>
  );
}

/* ------------------------------------------------------------------ */
/* AppShell                                                             */
/* ------------------------------------------------------------------ */

export function AppShell({ children }: { children: React.ReactNode }) {
  useLiveWs();
  const [palette, setPalette] = React.useState(false);
  const [mobileNav, setMobileNav] = React.useState(false);
  const router = useRouter();

  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPalette((v) => !v);
      }
      if (e.key === "Escape") {
        setPalette(false);
        setMobileNav(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  React.useEffect(() => {
    const onExport = () => {
      fetch("/api/trades?limit=2000")
        .then((r) => r.json())
        .then((d) => {
          const rows = d.trades ?? [];
          const header = ["id", "ts", "asset", "side", "strategy", "entry_price", "size_usd", "fee_usd", "status", "exit_ts", "exit_price", "exit_reason", "realized_pnl_usd"];
          const csv = [header.join(",")]
            .concat(
              rows.map((t: Record<string, unknown>) =>
                header.map((h) => JSON.stringify(t[h] ?? "")).join(",")
              )
            )
            .join("\n");
          const blob = new Blob([csv], { type: "text/csv" });
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          a.download = `trades-${new Date().toISOString().slice(0, 10)}.csv`;
          a.click();
          URL.revokeObjectURL(url);
        })
        .catch(() => {});
    };
    window.addEventListener("arb:export-csv", onExport);
    return () => window.removeEventListener("arb:export-csv", onExport);
  }, []);

  return (
    <div className="flex h-screen overflow-hidden">
      <aside className="hidden shrink-0 lg:block">
        <Sidebar />
      </aside>

      <AnimatePresence>
        {mobileNav && (
          <motion.div
            className="fixed inset-0 z-40 lg:hidden"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
          >
            <div className="absolute inset-0 bg-black/60" onClick={() => setMobileNav(false)} />
            <motion.div
              className="absolute inset-y-0 left-0"
              initial={{ x: -232 }}
              animate={{ x: 0 }}
              exit={{ x: -232 }}
              transition={{ type: "spring", damping: 28, stiffness: 300 }}
            >
              <Sidebar mobile onNavigate={() => setMobileNav(false)} />
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      <div className="flex min-w-0 flex-1 flex-col">
        <Topbar onMenu={() => setMobileNav(true)} onPalette={() => setPalette(true)} />
        <main className="flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-[1480px] px-4 py-6 lg:px-6">{children}</div>
        </main>
      </div>

      <CommandPalette open={palette} onClose={() => setPalette(false)} />
    </div>
  );
}
