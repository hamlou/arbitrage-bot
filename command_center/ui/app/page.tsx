"use client";

import * as React from "react";
import { motion } from "framer-motion";
import { useEquity, useOverview } from "@/lib/api";
import { SectionTitle } from "@/components/widgets";
import {
  ActivityFeed,
  HealthPanel,
  LatencyPanel,
  MarketsPanel,
  OverviewSkeleton,
  PositionsPanel,
  StatStrip,
  StrategyPanel,
} from "@/components/home";
import { EquityChart } from "@/components/charts";
import { Card, CardHeader } from "@/components/ui/primitives";
import { TrendingUp } from "lucide-react";

export default function OverviewPage() {
  const { data: ov, isLoading } = useOverview(2500);
  const { data: eq } = useEquity(3000);

  if (isLoading || !ov) return <OverviewSkeleton />;

  const motionWrap = (children: React.ReactNode, i: number) => (
    <motion.div key={i} custom={i} variants={stagger} initial="hidden" animate="show">
      {children}
    </motion.div>
  );

  return (
    <div className="space-y-4">
      <SectionTitle
        eyebrow={ov.bot_online ? "Live" : "Offline snapshot"}
        title="Command Center"
        desc="What is happening, what changed, what needs attention — the bot's full state, refreshed every two seconds."
      />

      <motion.div custom={0} variants={stagger} initial="hidden" animate="show">
        <StatStrip ov={ov} equity={eq?.points ?? []} />
      </motion.div>

      <div className="grid gap-4 xl:grid-cols-3">
        <motion.div custom={1} variants={stagger} initial="hidden" animate="show" className="xl:col-span-2">
          <Card>
            <CardHeader title="Profit Timeline" subtitle="Reconstructed from the trade ledger — every open and settlement" icon={<TrendingUp className="h-3.5 w-3.5" />} />
            <div className="p-2 pt-1">
              <EquityChart points={eq?.points ?? []} />
            </div>
          </Card>
        </motion.div>
        <motion.div custom={2} variants={stagger} initial="hidden" animate="show">
          <HealthPanel ov={ov} />
        </motion.div>
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <motion.div custom={3} variants={stagger} initial="hidden" animate="show" className="xl:col-span-2">
          <MarketsPanel ov={ov} />
        </motion.div>
        <motion.div custom={4} variants={stagger} initial="hidden" animate="show">
          <LatencyPanel ov={ov} />
        </motion.div>
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <motion.div custom={5} variants={stagger} initial="hidden" animate="show">
          <PositionsPanel positions={ov.positions} />
        </motion.div>
        <motion.div custom={6} variants={stagger} initial="hidden" animate="show">
          <StrategyPanel ov={ov} />
        </motion.div>
        <motion.div custom={7} variants={stagger} initial="hidden" animate="show">
          <ActivityFeed />
        </motion.div>
      </div>
    </div>
  );
}

const stagger = {
  hidden: { opacity: 0, y: 8 },
  show: (i: number) => ({
    opacity: 1,
    y: 0,
    transition: { delay: i * 0.05, duration: 0.4, ease: [0.16, 1, 0.3, 1] as const },
  }),
};
