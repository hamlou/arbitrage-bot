"use client";

import * as React from "react";
import { useActivity } from "@/lib/api";
import { fmtAgo, fmtDateTime, fmtUsd } from "@/lib/format";
import { Card, Skeleton, Badge } from "@/components/ui/primitives";
import { EmptyState, PnlText, SectionTitle } from "@/components/widgets";
import { Tabs } from "@/components/ui/overlays";
import type { ActivityItem } from "@/lib/types";

export default function ActivityPage() {
  const { data, isLoading } = useActivity();
  const [filter, setFilter] = React.useState("all");
  const items = (data?.items ?? []).filter((i) => filter === "all" || i.type === filter);

  const counts = {
    all: data?.items.length ?? 0,
    trade: (data?.items ?? []).filter((i) => i.type === "trade").length,
    signal: (data?.items ?? []).filter((i) => i.type === "signal").length,
    risk: (data?.items ?? []).filter((i) => i.type === "risk").length,
  };

  return (
    <div className="space-y-4">
      <SectionTitle
        eyebrow="Intelligence"
        title="Activity Stream"
        desc="The complete decision trail — every signal, fill, settlement and risk event, newest first."
        right={
          <Tabs
            value={filter}
            onChange={setFilter}
            tabs={[
              { key: "all", label: "All", count: counts.all },
              { key: "trade", label: "Trades", count: counts.trade },
              { key: "signal", label: "Signals", count: counts.signal },
              { key: "risk", label: "Risk", count: counts.risk },
            ]}
          />
        }
      />

      <Card className="p-4">
        {isLoading ? (
          <div className="space-y-3">
            {Array.from({ length: 10 }).map((_, i) => (
              <Skeleton key={i} className="h-12" />
            ))}
          </div>
        ) : items.length === 0 ? (
          <EmptyState title="Nothing here yet" desc="The stream fills with signals and trades as the bot runs." />
        ) : (
          <div className="mx-auto max-w-3xl">
            {items.map((item, i) => (
              <Row key={`${item.type}-${item.ts}-${i}`} item={item} last={i === items.length - 1} />
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}

function Row({ item, last }: { item: ActivityItem; last: boolean }) {
  const dotColor =
    item.type === "trade"
      ? "bg-brand"
      : item.type === "signal"
        ? "bg-info"
        : "bg-danger";

  return (
    <div className="relative flex gap-4 pb-6 last:pb-0">
      {!last && <span className="absolute left-[6px] top-5 h-full w-px bg-line" />}
      <span className={`relative mt-1 h-3 w-3 shrink-0 rounded-full border-2 border-surface ${dotColor}`} />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-3">
          <div className="flex min-w-0 items-center gap-2">
            <span className="truncate text-[13px] font-medium text-ink">{item.label}</span>
            {item.type === "signal" && item.edge_pct !== null && item.edge_pct !== undefined && (
              <Badge tone="info">+{(item.edge_pct * 100).toFixed(1)}% edge</Badge>
            )}
            {item.type === "trade" && item.exit_reason && item.exit_reason !== "SETTLED" && (
              <Badge tone={item.exit_reason === "TAKE_PROFIT" ? "ok" : "warn"}>{item.exit_reason}</Badge>
            )}
          </div>
          <span className="shrink-0 font-mono text-[11px] tabular text-ink-faint">{fmtAgo(item.ts)}</span>
        </div>
        <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-ink-faint">
          <span>{fmtDateTime(item.ts)}</span>
          {item.strategy && <span>· {item.strategy}</span>}
          {item.market_id && <span>· market {item.market_id.slice(0, 8)}…</span>}
          {item.size_usd !== null && item.size_usd !== undefined && <span>· size {fmtUsd(item.size_usd)}</span>}
          {item.detail && <span className="text-ink-muted">· {item.detail}</span>}
        </div>
        {item.type === "trade" && item.pnl_usd !== null && item.pnl_usd !== undefined && (
          <div className="mt-1">
            <PnlText value={item.pnl_usd} />
          </div>
        )}
      </div>
    </div>
  );
}
