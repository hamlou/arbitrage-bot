"use client";

import { SectionTitle } from "@/components/widgets";
import { TradesTable } from "@/components/trades-table";

export default function TradesPage() {
  return (
    <div className="space-y-4">
      <SectionTitle
        eyebrow="Execution"
        title="Trade Ledger"
        desc="Every fill, settlement and early exit — filter, sort, export, and click any row for the full picture including the model reads behind it."
      />
      <TradesTable />
    </div>
  );
}
