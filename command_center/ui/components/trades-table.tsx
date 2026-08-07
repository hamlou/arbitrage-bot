"use client";

import * as React from "react";
import { Download, Eye, EyeOff, Filter, Search, SlidersHorizontal } from "lucide-react";
import { cn } from "@/lib/utils";
import { useSignals, useTrades } from "@/lib/api";
import type { Trade } from "@/lib/types";
import { fmtAgo, fmtDateTime, fmtUsd, shortId } from "@/lib/format";
import { Badge, Button, Input, Kbd, Skeleton, Card, Separator } from "@/components/ui/primitives";
import { EmptyState, PnlText } from "@/components/widgets";
import { Drawer, Select } from "@/components/ui/overlays";

type SortKey = "entry_ts" | "exit_ts" | "realized_pnl_usd" | "size_usd" | "entry_price";

const COLUMNS: { key: string; label: string }[] = [
  { key: "id", label: "#" },
  { key: "time", label: "Time" },
  { key: "asset", label: "Asset" },
  { key: "side", label: "Side" },
  { key: "strategy", label: "Strategy" },
  { key: "entry", label: "Entry" },
  { key: "exit", label: "Exit" },
  { key: "size", label: "Size" },
  { key: "pnl", label: "PnL" },
  { key: "reason", label: "Exit" },
];

export function TradesTable() {
  const [status, setStatus] = React.useState("all");
  const [strategy, setStrategy] = React.useState("all");
  const [side, setSide] = React.useState("all");
  const [asset, setAsset] = React.useState("all");
  const [q, setQ] = React.useState("");
  const [sortKey, setSortKey] = React.useState<SortKey>("entry_ts");
  const [sortDir, setSortDir] = React.useState<"asc" | "desc">("desc");
  const [hiddenCols, setHiddenCols] = React.useState<Set<string>>(new Set());
  const [showCols, setShowCols] = React.useState(false);
  const [selected, setSelected] = React.useState<Trade | null>(null);

  const { data, isLoading } = useTrades({ status, strategy, side, asset, limit: "200" });

  const trades = React.useMemo(() => {
    let list = [...(data?.trades ?? [])];
    if (q.trim()) {
      const s = q.trim().toLowerCase();
      list = list.filter(
        (t) =>
          t.market_id.toLowerCase().includes(s) ||
          t.asset.toLowerCase().includes(s) ||
          t.strategy.toLowerCase().includes(s) ||
          String(t.id).includes(s)
      );
    }
    list.sort((a, b) => {
      const av = a[sortKey] ?? 0;
      const bv = b[sortKey] ?? 0;
      return sortDir === "asc" ? av - bv : bv - av;
    });
    return list;
  }, [data, q, sortKey, sortDir]);

  const stats = data?.stats;

  const toggleCol = (key: string) => {
    setHiddenCols((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const toggleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir((d) => (d === "desc" ? "asc" : "desc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  };

  const exportCsv = () => {
    const header = ["id", "entry_ts", "exit_ts", "asset", "side", "strategy", "entry_price", "exit_price", "size_usd", "fee_usd", "realized_pnl_usd", "exit_reason", "status"];
    const csv = [header.join(",")]
      .concat(trades.map((t) => header.map((h) => JSON.stringify(t[h as keyof Trade] ?? "")).join(",")))
      .join("\n");
    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `trades-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const hidden = (k: string) => hiddenCols.has(k);

  return (
    <>
      <Card className="overflow-hidden">
        {/* toolbar */}
        <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-ink-faint" />
            <Input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Filter by market, asset, id…"
              className="w-56 pl-8"
            />
          </div>
          <Select
            value={status}
            onChange={setStatus}
            options={[
              { value: "all", label: "All statuses" },
              { value: "OPEN", label: "Open" },
              { value: "CLOSED", label: "Closed" },
            ]}
          />
          <Select
            value={strategy}
            onChange={setStrategy}
            options={[
              { value: "all", label: "All strategies" },
              { value: "latency_arb", label: "Latency arb" },
              { value: "sum_to_one", label: "Sum-to-one" },
            ]}
          />
          <Select
            value={side}
            onChange={setSide}
            options={[
              { value: "all", label: "All sides" },
              { value: "YES", label: "YES" },
              { value: "NO", label: "NO" },
            ]}
          />
          <Select
            value={asset}
            onChange={setAsset}
            options={[
              { value: "all", label: "All assets" },
              { value: "BTC", label: "BTC" },
              { value: "ETH", label: "ETH" },
            ]}
          />

          <div className="ml-auto flex items-center gap-2">
            <div className="flex items-center gap-3 rounded-lg border border-line bg-raised px-3 py-1.5 font-mono text-[11px] tabular">
              <span className="text-ink-faint">Σ</span>
              <span className={cn(stats && (stats.closed_pnl_usd >= 0 ? "text-ok" : "text-danger"))}>
                {fmtUsd(stats?.closed_pnl_usd ?? 0, { sign: true })}
              </span>
              <span className="text-ink-faint">|</span>
              <span className="text-ink-muted">{stats?.wins ?? 0}W</span>
              <span className="text-ink-muted">{stats?.losses ?? 0}L</span>
            </div>
            <Button variant="secondary" size="sm" onClick={() => setShowCols((v) => !v)}>
              <SlidersHorizontal className="h-3.5 w-3.5" /> Columns
            </Button>
            <Button variant="secondary" size="sm" onClick={exportCsv}>
              <Download className="h-3.5 w-3.5" /> CSV
            </Button>
          </div>
        </div>

        {/* column visibility */}
        {showCols && (
          <div className="flex flex-wrap gap-1.5 border-b border-line bg-raised/50 px-4 py-2">
            {COLUMNS.map((c) => (
              <button
                key={c.key}
                onClick={() => toggleCol(c.key)}
                className={cn(
                  "flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] transition-colors",
                  hidden(c.key)
                    ? "border-line text-ink-faint"
                    : "border-brand/30 bg-brand-soft text-brand"
                )}
              >
                {hidden(c.key) ? <EyeOff className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
                {c.label}
              </button>
            ))}
          </div>
        )}

        {/* table */}
        <div className="max-h-[560px] overflow-auto">
          <table className="w-full border-collapse text-[12px]">
            <thead className="sticky top-0 z-10 bg-surface">
              <tr className="text-left text-[10px] uppercase tracking-wider text-ink-faint">
                {!hidden("id") && <Th>#</Th>}
                {!hidden("time") && <Th onClick={() => toggleSort("entry_ts")}>Opened{sortKey === "entry_ts" ? (sortDir === "asc" ? " ↑" : " ↓") : ""}</Th>}
                {!hidden("asset") && <Th>Asset</Th>}
                {!hidden("side") && <Th>Side</Th>}
                {!hidden("strategy") && <Th>Strategy</Th>}
                {!hidden("entry") && <Th className="text-right">Entry</Th>}
                {!hidden("exit") && <Th className="text-right">Exit</Th>}
                {!hidden("size") && <Th className="text-right">Size</Th>}
                {!hidden("pnl") && (
                  <Th className="text-right" onClick={() => toggleSort("realized_pnl_usd")}>
                    PnL{sortKey === "realized_pnl_usd" ? (sortDir === "asc" ? " ↑" : " ↓") : ""}
                  </Th>
                )}
                {!hidden("reason") && <Th>Exit</Th>}
              </tr>
            </thead>
            <tbody>
              {isLoading ? (
                Array.from({ length: 8 }).map((_, i) => (
                  <tr key={i} className="border-t border-line/60">
                    <td colSpan={10} className="px-3 py-2">
                      <Skeleton className="h-7" />
                    </td>
                  </tr>
                ))
              ) : trades.length === 0 ? (
                <tr>
                  <td colSpan={10}>
                    <EmptyState
                      title="No trades match"
                      desc="Adjust filters or wait for the bot to trade."
                    />
                  </td>
                </tr>
              ) : (
                trades.map((t) => (
                  <tr
                    key={t.id}
                    onClick={() => setSelected(t)}
                    className="cursor-pointer border-t border-line/60 transition-colors hover:bg-raised/70"
                  >
                    {!hidden("id") && <td className="px-3 py-2 font-mono text-[11px] text-ink-faint tabular">{t.id}</td>}
                    {!hidden("time") && (
                      <td className="whitespace-nowrap px-3 py-2 font-mono text-[11px] tabular text-ink-muted">
                        {fmtDateTime(t.entry_ts)}
                      </td>
                    )}
                    {!hidden("asset") && (
                      <td className="px-3 py-2">
                        <span className="font-mono text-xs font-semibold text-ink">{t.asset}</span>
                      </td>
                    )}
                    {!hidden("side") && (
                      <td className="px-3 py-2">
                        <Badge tone={t.side === "YES" ? "ok" : "danger"}>{t.side}</Badge>
                      </td>
                    )}
                    {!hidden("strategy") && (
                      <td className="px-3 py-2 text-[11px] text-ink-muted">
                        {t.strategy === "sum_to_one" ? "sum-to-one" : "latency"}
                      </td>
                    )}
                    {!hidden("entry") && (
                      <td className="px-3 py-2 text-right font-mono tabular text-ink">{t.entry_price.toFixed(3)}</td>
                    )}
                    {!hidden("exit") && (
                      <td className="px-3 py-2 text-right font-mono tabular text-ink-muted">
                        {t.exit_price !== null ? t.exit_price.toFixed(3) : "—"}
                      </td>
                    )}
                    {!hidden("size") && (
                      <td className="px-3 py-2 text-right font-mono tabular text-ink-muted">{fmtUsd(t.size_usd)}</td>
                    )}
                    {!hidden("pnl") && (
                      <td className="px-3 py-2 text-right">
                        <PnlText value={t.status === "CLOSED" ? t.realized_pnl_usd : null} />
                      </td>
                    )}
                    {!hidden("reason") && (
                      <td className="px-3 py-2">
                        <span
                          className={cn(
                            "rounded-full px-2 py-0.5 text-[10px] font-medium",
                            t.status === "OPEN"
                              ? "bg-brand-soft text-brand"
                              : t.exit_reason === "TAKE_PROFIT"
                                ? "bg-ok-soft text-ok"
                                : t.exit_reason === "SETTLED"
                                  ? "bg-raised text-ink-muted"
                                  : "bg-warn-soft text-warn"
                          )}
                        >
                          {t.status === "OPEN" ? "OPEN" : (t.exit_reason ?? "CLOSED")}
                        </span>
                      </td>
                    )}
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        <div className="flex items-center justify-between border-t border-line px-4 py-2.5 text-[11px] text-ink-faint">
          <span>
            Showing {trades.length} of {data?.stats.total ?? 0} trades
          </span>
          <span className="flex items-center gap-1.5">
            Click a row for full detail <Kbd>esc</Kbd> closes
          </span>
        </div>
      </Card>

      <TradeDetail trade={selected} onClose={() => setSelected(null)} />
    </>
  );
}

function Th({
  children,
  onClick,
  className,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  className?: string;
}) {
  return (
    <th
      onClick={onClick}
      className={cn("cursor-pointer select-none px-3 py-2 font-medium", onClick && "hover:text-ink", className)}
    >
      {children}
    </th>
  );
}

/* ------------------------------------------------------------------ */
/* Trade detail drawer                                                  */
/* ------------------------------------------------------------------ */

function TradeDetail({ trade, onClose }: { trade: Trade | null; onClose: () => void }) {
  const { data: sigData } = useSignals(false, 200);
  const signals = trade
    ? (sigData?.signals ?? []).filter((s) => s.market_id === trade.market_id).slice(0, 8)
    : [];

  return (
    <Drawer open={!!trade} onClose={onClose} title={trade ? `Trade #${trade.id}` : ""}>
      {trade && (
        <div className="space-y-5">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <span className="font-mono text-lg font-bold text-ink">{trade.asset}</span>
              <Badge tone={trade.side === "YES" ? "ok" : "danger"}>{trade.side}</Badge>
              <Badge tone={trade.status === "OPEN" ? "info" : "neutral"}>{trade.status}</Badge>
            </div>
            <PnlText value={trade.status === "CLOSED" ? trade.realized_pnl_usd : null} />
          </div>

          <div className="grid grid-cols-2 gap-2">
            <DetailCell label="Market" v={shortId(trade.market_id)} mono />
            <DetailCell label="Strategy" v={trade.strategy === "sum_to_one" ? "sum-to-one" : "latency arb"} />
            <DetailCell label="Opened" v={fmtDateTime(trade.entry_ts)} />
            <DetailCell label="Entry price" v={`$${trade.entry_price.toFixed(4)}`} mono />
            <DetailCell label="Size" v={fmtUsd(trade.size_usd)} mono />
            <DetailCell label="Fee" v={fmtUsd(trade.fee_usd)} mono />
            {trade.status === "CLOSED" && (
              <>
                <DetailCell label="Closed" v={fmtDateTime(trade.exit_ts)} />
                <DetailCell label="Exit price" v={trade.exit_price !== null ? `$${trade.exit_price.toFixed(4)}` : "—"} mono />
                <DetailCell label="Exit reason" v={trade.exit_reason ?? "—"} tone={trade.exit_reason === "TAKE_PROFIT" ? "ok" : "neutral"} />
                <DetailCell
                  label="Age at close"
                  v={fmtAgo(trade.exit_ts ?? trade.entry_ts)}
                />
              </>
            )}
          </div>

          <Separator />

          <div>
            <div className="mb-2 flex items-center justify-between">
              <h4 className="text-xs font-semibold text-ink">Signal history for this market</h4>
              <span className="text-[10px] text-ink-faint">model reads at decision time</span>
            </div>
            {signals.length === 0 ? (
              <p className="text-xs text-ink-faint">No signals logged for this market.</p>
            ) : (
              <div className="space-y-1.5">
                {signals.map((s) => (
                  <div
                    key={s.id}
                    className="flex items-center justify-between rounded-lg border border-line bg-raised px-3 py-2 text-[11px]"
                  >
                    <span className="font-mono tabular text-ink-faint">{fmtDateTime(s.ts)}</span>
                    <span className="font-mono tabular text-ink">
                      {s.implied_prob.toFixed(3)} → <span className="text-ink-muted">{s.polymarket_prob.toFixed(3)}</span>
                    </span>
                    <span className={cn("font-mono tabular", s.fired ? "text-brand" : "text-ink-faint")}>
                      {s.fired ? `fire ${(s.edge_pct * 100).toFixed(1)}%` : "no fire"}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>

          {trade.combo_group_id && (
            <div className="rounded-lg border border-info/25 bg-info-soft px-3 py-2 text-[11px] text-info">
              Part of sum-to-one pair {shortId(trade.combo_group_id)} — profit locked at entry regardless of outcome.
            </div>
          )}
        </div>
      )}
    </Drawer>
  );
}

function DetailCell({
  label,
  v,
  mono,
  tone,
}: {
  label: string;
  v: string;
  mono?: boolean;
  tone?: "ok" | "neutral";
}) {
  return (
    <div className="rounded-lg border border-line bg-raised px-3 py-2">
      <div className="text-[9px] uppercase tracking-wider text-ink-faint">{label}</div>
      <div
        className={cn(
          "mt-0.5 truncate text-xs",
          mono && "font-mono tabular",
          tone === "ok" ? "text-ok" : "text-ink"
        )}
      >
        {v}
      </div>
    </div>
  );
}
