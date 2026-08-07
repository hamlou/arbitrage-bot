"use client";

import { useMarkets, useOverview } from "@/lib/api";
import { fmtDuration, fmtUsd } from "@/lib/format";
import { Badge, Card } from "@/components/ui/primitives";
import { EmptyState, SectionTitle, useTick } from "@/components/widgets";
import { CandlestickChart, Timer } from "lucide-react";

export default function MarketsPage() {
  const { data } = useMarkets();
  const { data: ov } = useOverview(5000);
  const now = useTick(1000);
  const markets = data?.markets ?? [];

  const nowS = now / 1000;
  const live = markets.filter((m) => (m.expires_at_ts ?? 0) > nowS);
  const soon = markets.filter((m) => (m.expires_at_ts ?? 0) > nowS).sort(
    (a, b) => (a.expires_at_ts ?? 0) - (b.expires_at_ts ?? 0)
  );
  const totalLiq = markets.reduce((a, m) => a + (m.liquidity_usd ?? 0), 0);
  const sumToOne = markets.filter((m) => (m.yes_mid ?? 0) + (m.no_mid ?? 0) > 0 && (m.yes_mid ?? 0) + (m.no_mid ?? 0) < 0.995);

  return (
    <div className="space-y-4">
      <SectionTitle
        eyebrow="Execution"
        title="Market Snapshot"
        desc={`${markets.length} tradeable windows discovered${ov?.bot_online ? " — streamed live" : ""}.`}
      />

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Windows" value={String(markets.length)} />
        <Stat label="Live now" value={String(live.length)} tone={live.length > 0 ? "ok" : "muted"} />
        <Stat label="Sum-to-one candidates" value={String(sumToOne.length)} />
        <Stat label="Total liquidity" value={fmtUsd(totalLiq)} />
      </div>

      <Card>
        {markets.length === 0 ? (
          <EmptyState
            icon={<CandlestickChart />}
            title="No windows found right now"
            desc="This is normal between windows — a fresh 5 or 15-minute window opens every few minutes. The bot re-discovers continuously."
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-[12px]">
              <thead>
                <tr className="text-left text-[10px] uppercase tracking-wider text-ink-faint">
                  <th className="px-4 py-3 font-medium">Asset</th>
                  <th className="px-4 py-3 font-medium">Market</th>
                  <th className="px-4 py-3 font-medium">Closes</th>
                  <th className="px-4 py-3 text-right font-medium">YES</th>
                  <th className="px-4 py-3 text-right font-medium">NO</th>
                  <th className="px-4 py-3 text-right font-medium">Σ edge</th>
                  <th className="px-4 py-3 text-right font-medium">Liquidity</th>
                  <th className="px-4 py-3 text-right font-medium">Reference</th>
                </tr>
              </thead>
              <tbody>
                {soon.map((m) => {
                  const sum = (m.yes_mid ?? 0) + (m.no_mid ?? 0);
                  const edge = sum > 0 ? Math.max(0, (1 - sum) * 100) : 0;
                  const remaining = m.expires_at_ts ? m.expires_at_ts - nowS : null;
                  return (
                    <tr key={m.market_id} className="border-t border-line/60 transition-colors hover:bg-raised/60">
                      <td className="px-4 py-3">
                        <span className="inline-flex items-center gap-1.5">
                          <span className="flex h-6 w-6 items-center justify-center rounded bg-brand-soft font-mono text-[10px] font-bold text-brand">
                            {m.asset.slice(0, 1)}
                          </span>
                          <span className="font-mono text-xs font-semibold text-ink">{m.asset}</span>
                          <Badge tone={m.duration_minutes === 5 ? "info" : "brand"}>{m.duration_minutes}m</Badge>
                        </span>
                      </td>
                      <td className="max-w-[340px] truncate px-4 py-3 text-ink-muted" title={m.question}>
                        {m.question}
                      </td>
                      <td className="px-4 py-3">
                        <span className="inline-flex items-center gap-1 font-mono tabular text-ink">
                          <Timer className="h-3 w-3 text-ink-faint" />
                          {remaining !== null ? fmtDuration(remaining) : "—"}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-right font-mono tabular text-ok">
                        {m.yes_mid !== null ? m.yes_mid.toFixed(3) : "—"}
                      </td>
                      <td className="px-4 py-3 text-right font-mono tabular text-danger">
                        {m.no_mid !== null ? m.no_mid.toFixed(3) : "—"}
                      </td>
                      <td className="px-4 py-3 text-right">
                        {edge > 0.5 ? (
                          <Badge tone="ok">+{edge.toFixed(2)}%</Badge>
                        ) : (
                          <span className="text-ink-faint">—</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-right font-mono tabular text-ink-muted">{fmtUsd(m.liquidity_usd)}</td>
                      <td className="px-4 py-3 text-right font-mono tabular text-ink-faint">
                        {m.reference_price ? `$${m.reference_price.toLocaleString("en-US", { maximumFractionDigits: 0 })}` : "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <p className="text-[11px] leading-relaxed text-ink-faint">
        <span className="font-semibold text-ink-muted">How to read this:</span> YES/NO mids are the current order-book
        mid prices streamed from Polymarket. <span className="text-ok">Σ edge</span> is the risk-free sum-to-one
        arbitrage — if YES + NO costs under $1, buying both sides locks in the difference at settlement. Liquidity is
        Gamma's reported depth; the bot actually trades against the live book.
      </p>
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: "ok" | "muted" }) {
  return (
    <Card className="p-4">
      <div className="text-[10px] font-medium uppercase tracking-[0.1em] text-ink-faint">{label}</div>
      <div className={`mt-1 font-mono text-lg font-semibold tabular ${tone === "ok" ? "text-ok" : tone === "muted" ? "text-ink-muted" : "text-ink"}`}>
        {value}
      </div>
    </Card>
  );
}
