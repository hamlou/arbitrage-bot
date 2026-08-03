"""
Latency instrumentation: measures how long it actually takes to go from a
Binance tick to an evaluated signal to a (would-be) order, so that number can
be compared against the real arbitrage window instead of assumed. This was
previously never measured — see the project's continuation-planning notes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from storage.db import Database


@dataclass
class LatencyCycle:
    """One in-flight measurement, from tick received to (optionally) order submitted."""
    market_id: str
    tick_received_at: float
    signal_evaluated_at: Optional[float] = None
    order_submitted_at: Optional[float] = None

    def mark_signal_evaluated(self) -> None:
        self.signal_evaluated_at = time.time()

    def mark_order_submitted(self) -> None:
        self.order_submitted_at = time.time()

    @property
    def tick_to_signal_ms(self) -> Optional[float]:
        if self.signal_evaluated_at is None:
            return None
        return (self.signal_evaluated_at - self.tick_received_at) * 1000

    @property
    def tick_to_order_ms(self) -> Optional[float]:
        if self.order_submitted_at is None:
            return None
        return (self.order_submitted_at - self.tick_received_at) * 1000


class LatencyTracker:
    """
    Thin wrapper that starts a LatencyCycle and persists it to storage once
    complete. Usage in a trading cycle:

        cycle = tracker.start(market_id, tick_received_at=tracker_snapshot_time)
        signal = await signal_engine.evaluate(market, book)
        cycle.mark_signal_evaluated()
        if signal.fired:
            await broker.place_order(...)
            cycle.mark_order_submitted()
        await tracker.finish(cycle, fired=signal.fired)
    """

    def __init__(self, db: Database):
        self.db = db

    def start(self, market_id: str, tick_received_at: Optional[float] = None) -> LatencyCycle:
        return LatencyCycle(market_id=market_id, tick_received_at=tick_received_at or time.time())

    async def finish(self, cycle: LatencyCycle, fired: bool) -> None:
        if cycle.signal_evaluated_at is None:
            cycle.mark_signal_evaluated()
        await self.db.log_latency_event(
            market_id=cycle.market_id,
            tick_received_at=cycle.tick_received_at,
            signal_evaluated_at=cycle.signal_evaluated_at,
            order_submitted_at=cycle.order_submitted_at,
            fired=fired,
        )
