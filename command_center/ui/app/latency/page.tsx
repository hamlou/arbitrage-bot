"use client";

import * as React from "react";
import { useLatency, useOverview } from "@/lib/api";
import { fmtMs } from "@/lib/format";
import { Badge, Card, CardHeader, Skeleton } from "@/components/ui/primitives";
import { SectionTitle } from "@/components/widgets";
import { LatencyBudget } from "@/components/charts";
import { Timer } from "lucide-react";

const PERCENTILES = [
  { label: "p50", key: "p50_ms" },
  { label: "p75", key: "p75_ms" },
  { label: "p95", key: "p95_ms" },
  { label: "p99", key: "p99_ms" },
  { label: "max", key: "max_ms" },
];

export default function LatencyPage() {
  const { data, isLoading } = useLatency();
  const { data: ov } = useOverview(5000);

  const windowMs = (ov?.latency.window_s ?? 2.0) * 1000;
  const platformMs = ov?.latency.platform_delay_ms ?? 250;
  const p95Order = data?.tick_to_order.p95_ms ?? null;
  const verdict = p95Order === null ? "n/a" : p95Order + platformMs < windowMs * 0.5 ? "comfortable" : p95Order + platformMs < windowMs ? "tight" : "too slow";
  const verdictTone = verdict === "comfortable" ? ("ok" as const) : verdict === "tight" ? ("warn" as const) : ("danger" as const);

  return (
    <div className="space-y-4">
      <SectionTitle
        eyebrow="Intelligence"
        title="Timing Analysis"
        desc="The measured pipeline speed versus the real arbitrage window — this is the answer to “can the bot win the gap?”"
      />

      <div className="grid gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader
            title="Latency Distribution"
            subtitle="Time from Binance tick → signal → order submission"
            icon={<Timer className="h-3.5 w-3.5" />}
            action={<Badge tone={verdictTone}>{verdict}</Badge>}
          />
          <div className="p-4">
            <div className="grid grid-cols-2 gap-3">
              <PctBlock title="tick → signal" data={data?.tick_to_signal} isLoading={isLoading} />
              <PctBlock title="tick → order" data={data?.tick_to_order} isLoading={isLoading} />
            </div>
            <div className="mt-4 rounded-lg border border-line bg-raised p-3">
              <div className="mb-1.5 text-[11px] text-ink-muted">Budget vs {Math.round(windowMs)}ms window</div>
              <LatencyBudget
                tickToSignalP95={data?.tick_to_signal.p95_ms ?? null}
                signalToOrderP95={data?.signal_to_order.p95_ms ?? null}
                platformDelayMs={platformMs}
                windowMs={windowMs}
              />
            </div>
          </div>
        </Card>

        <Card>
          <CardHeader title="Interpretation" subtitle="What the numbers mean for profitability" />
          <div className="space-y-3 px-4 pb-4 pt-1 text-[12px] leading-relaxed text-ink-muted">
            <p>
              The window is how long Polymarket's price lags Binance after a move —
              estimated at <span className="font-mono text-ink tabular">~2s</span> from the
              OpenMarket dataset. On top of that, Polymarket holds fast taker orders for{" "}
              <span className="font-mono text-ink tabular">{Math.round(platformMs)}ms</span>.
            </p>
            <p>
              As long as <span className="font-mono text-ink tabular">tick→order p95 + {Math.round(platformMs)}ms</span>{" "}
              fits inside the window with room to spare, latency is not the constraint —
              model quality and entry price discipline are.
            </p>
            <div className="rounded-lg border border-info/25 bg-info-soft px-3 py-2 text-[11px] text-info">
              {data?.fired ?? 0} of {(data?.count ?? 0)} measured cycles produced an order.
            </div>
          </div>
        </Card>
      </div>

      <Card>
        <CardHeader title="Recent measured cycles" subtitle="Every logged latency sample (fired = an order was submitted)" />
        <div className="overflow-x-auto px-4 pb-4 pt-1">
          <SeriesTable />
        </div>
      </Card>
    </div>
  );
}

function PctBlock({
  title,
  data,
  isLoading,
}: {
  title: string;
  data: Record<string, number | null> | undefined;
  isLoading: boolean;
}) {
  const max = Math.max(1, ...(PERCENTILES.map((p) => data?.[p.key] ?? 0)));
  return (
    <div className="rounded-lg border border-line bg-raised p-3">
      <div className="mb-2 text-[11px] font-medium text-ink">{title}</div>
      {isLoading ? (
        <Skeleton className="h-24" />
      ) : (
        <div className="space-y-2">
          {PERCENTILES.map((p) => {
            const v = data?.[p.key];
            return (
              <div key={p.key} className="flex items-center gap-2">
                <span className="w-8 font-mono text-[10px] text-ink-faint">{p.label}</span>
                <div className="h-3 flex-1 overflow-hidden rounded bg-raised">
                  <div
                    className="h-full rounded bg-brand/70 transition-all duration-500"
                    style={{ width: `${((v ?? 0) / (max * 1.1)) * 100}%` }}
                  />
                </div>
                <span className="w-16 text-right font-mono text-[11px] tabular text-ink">{fmtMs(v)}</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function SeriesTable() {
  const { data } = useLatency();
  const rows = data?.series ?? [];
  return (
    <table className="w-full border-collapse text-[11px]">
      <thead>
        <tr className="text-left text-[10px] uppercase tracking-wider text-ink-faint">
          <th className="py-2 pr-3 font-medium">#</th>
          <th className="py-2 pr-3 font-medium">Market</th>
          <th className="py-2 pr-3 text-right font-medium">tick → signal</th>
          <th className="py-2 pr-3 text-right font-medium">tick → order</th>
          <th className="py-2 text-right font-medium">Fired</th>
        </tr>
      </thead>
      <tbody>
        {rows.slice(-40).reverse().map((r) => (
          <tr key={r.id} className="border-t border-line/60">
            <td className="py-1.5 pr-3 font-mono text-ink-faint tabular">{r.id}</td>
            <td className="max-w-[120px] truncate py-1.5 pr-3 font-mono text-ink-muted">{r.market_id}</td>
            <td className="py-1.5 pr-3 text-right font-mono tabular text-ink">{fmtMs(r.tick_to_signal_ms)}</td>
            <td className="py-1.5 pr-3 text-right font-mono tabular text-ink">{fmtMs(r.tick_to_order_ms)}</td>
            <td className="py-1.5 text-right">
              {r.fired ? <Badge tone="ok">fired</Badge> : <span className="text-ink-faint">—</span>}
            </td>
          </tr>
        ))}
        {rows.length === 0 && (
          <tr>
            <td colSpan={5} className="py-6 text-center text-ink-faint">
              No latency samples yet — they accumulate the moment the bot starts trading.
            </td>
          </tr>
        )}
      </tbody>
    </table>
  );
}
