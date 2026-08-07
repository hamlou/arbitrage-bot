"use client";

import { useOverview, usePositions } from "@/lib/api";
import { fmtUsd } from "@/lib/format";
import { Badge, Card, CardHeader, Progress, Skeleton } from "@/components/ui/primitives";
import { EmptyState, PnlText, SectionTitle } from "@/components/widgets";
import { Briefcase } from "lucide-react";

export default function PositionsPage() {
  const { data: pos } = usePositions();
  const { data: ov } = useOverview(5000);

  const positions = pos?.positions ?? [];
  const totalExposure = positions.reduce((a, p) => a + (p.size_usd ?? 0), 0);
  const totalUnrealized = positions.reduce((a, p) => a + (p.unrealized_pnl_usd ?? 0), 0);
  const equity = ov?.account.equity_usd ?? 1000;
  const exposurePct = equity > 0 ? (totalExposure / equity) * 100 : 0;

  return (
    <div className="space-y-4">
      <SectionTitle
        eyebrow="Execution"
        title="Open Positions"
        desc="Current exposure, marked to the live order book every cycle."
      />

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Summary label="Count" value={String(positions.length)} />
        <Summary label="Notional exposure" value={fmtUsd(totalExposure)} />
        <Summary label="% of equity" value={`${exposurePct.toFixed(1)}%`} sub="cap 30%" />
        <Summary label="Unrealized PnL" value={fmtUsd(totalUnrealized, { sign: true })} tone={totalUnrealized >= 0 ? "ok" : "danger"} />
      </div>

      <Card>
        <CardHeader
          title="Exposure vs cap"
          subtitle="Total open notional as a fraction of equity (MAX_TOTAL_EXPOSURE_PCT = 30%)"
          icon={<Briefcase className="h-3.5 w-3.5" />}
        />
        <div className="px-4 pb-4 pt-1">
          <Progress value={exposurePct} tone={exposurePct > 75 ? "danger" : exposurePct > 50 ? "warn" : "ok"} />
          <div className="mt-1 flex justify-between font-mono text-[10px] text-ink-faint tabular">
            <span>0%</span>
            <span>{exposurePct.toFixed(1)}% now</span>
            <span>30% cap</span>
          </div>
        </div>
      </Card>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {!pos
          ? Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-[150px]" />)
          : positions.length === 0
            ? (
                <div className="md:col-span-2 xl:col-span-3">
                  <Card>
                    <EmptyState
                      icon={<Briefcase />}
                      title="No open positions"
                      desc="When a signal fires, the position appears here with its live mark price and unrealized PnL."
                    />
                  </Card>
                </div>
              )
            : positions.map((p) => {
                const pnlPct = p.entry_price ? ((p.mark_price - p.entry_price) / p.entry_price) * 100 : 0;
                return (
                  <Card key={p.trade_id} hover className="p-4">
                    <div className="flex items-start justify-between">
                      <div>
                        <div className="flex items-center gap-2">
                          <span className="font-mono text-sm font-bold text-ink">{p.asset}</span>
                          <Badge tone={p.side === "YES" ? "ok" : "danger"}>{p.side}</Badge>
                          <Badge tone="neutral">{p.strategy === "sum_to_one" ? "sum-to-one" : "latency"}</Badge>
                        </div>
                        <div className="mt-1 font-mono text-[11px] text-ink-faint">#{p.trade_id} · {p.market_id.slice(0, 8)}…</div>
                      </div>
                      <div className="text-right">
                        <PnlText value={p.unrealized_pnl_usd} />
                        <div className={`mt-0.5 font-mono text-[10px] tabular ${pnlPct >= 0 ? "text-ok" : "text-danger"}`}>
                          {pnlPct >= 0 ? "+" : ""}{pnlPct.toFixed(2)}%
                        </div>
                      </div>
                    </div>
                    <div className="mt-3 grid grid-cols-4 gap-2 rounded-lg border border-line bg-raised p-2.5 text-center">
                      <Cell label="Entry" v={p.entry_price.toFixed(3)} />
                      <Cell label="Mark" v={p.mark_price.toFixed(3)} />
                      <Cell label="Size" v={fmtUsd(p.size_usd)} />
                      <Cell label="Fee" v={fmtUsd(p.fee_usd)} />
                    </div>
                  </Card>
                );
              })}
      </div>
    </div>
  );
}

function Summary({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "ok" | "danger" }) {
  return (
    <Card className="p-4">
      <div className="text-[10px] font-medium uppercase tracking-[0.1em] text-ink-faint">{label}</div>
      <div className={`mt-1 font-mono text-lg font-semibold tabular ${tone === "ok" ? "text-ok" : tone === "danger" ? "text-danger" : "text-ink"}`}>
        {value}
      </div>
      {sub && <div className="mt-0.5 text-[10px] text-ink-faint">{sub}</div>}
    </Card>
  );
}

function Cell({ label, v }: { label: string; v: string }) {
  return (
    <div>
      <div className="text-[9px] uppercase tracking-wider text-ink-faint">{label}</div>
      <div className="mt-0.5 font-mono text-[11px] tabular text-ink">{v}</div>
    </div>
  );
}
