"""
Lag-gap measurement tracker (pure diagnostics — never gates trading).

The strategy's premise is that Polymarket reprices BTC/ETH up-down contracts
with a delay after Binance prints a move. For a long time that delay was a
*guess* (the code assumed a 2.0s window, itself borrowed from a dataset).
This tracker measures it on the actual connection: for every Binance move
above a threshold, capture the direction-implied token's mid at move time,
then measure how long until that mid moves by at least `min_reprice_move`
(absolute price units, e.g. 0.005 = half a cent). The result is a lag
measurement per move — the empirical arbitrage window.

Design notes:
- Direction-aware: an UP move expects the YES mid to rise; a DOWN move
  expects the NO mid to rise. The tracker watches the direction-implied
  token and detects movement of at least min_reprice_move from the baseline
  captured at move time. A move in EITHER direction counts as "the market
  responded" — the actual direction is recorded in poly_move_pct so
  wrong-direction repricings are visible in the data.
- Timeout: moves that never reprice within timeout_s are finalized as
  timed_out (lag_ms = None) so the market's non-response is also visible.
- Bounded: max_pending caps the queue (oldest dropped first) so a flood of
  micro-moves can't grow memory without limit.
- Pure state machine with an injectable clock — no I/O, no network. main.py
  wires it to the Binance ingest loop and the Polymarket WS book cache and
  persists finalized measurements to the lag_events table.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LagMeasurement:
    """One finalized measurement: a Binance move and how long Polymarket
    took to respond (or timed_out=True if it never did)."""

    asset: str
    move_pct: float
    move_dir: str                # UP / DOWN — direction of the Binance move
    token_id: str                # the direction-implied token that should reprice
    binance_move_ts: float       # when the move was received locally
    baseline_mid: float          # implied token mid at move time
    poly_repriced_ts: Optional[float]
    poly_move_pct: Optional[float]  # actual mid change at detection (sign matters)
    timed_out: bool
    lag_ms: Optional[float]

    def to_db_row(self) -> dict:
        return {
            "asset": self.asset,
            "move_pct": self.move_pct,
            "move_dir": self.move_dir,
            "token_id": self.token_id,
            "binance_move_ts": self.binance_move_ts,
            "baseline_mid": self.baseline_mid,
            "poly_repriced_ts": self.poly_repriced_ts,
            "poly_move_pct": self.poly_move_pct,
            "timed_out": int(self.timed_out),
            "lag_ms": self.lag_ms,
        }


@dataclass(slots=True)
class _PendingMove:
    asset: str
    move_pct: float
    move_dir: str
    token_id: str
    baseline_mid: float
    binance_move_ts: float
    expires_at: float


class LagTracker:
    def __init__(
        self,
        min_reprice_move: float = 0.005,
        timeout_s: float = 30.0,
        max_pending: int = 200,
        clock=time.time,
    ):
        self.min_reprice_move = min_reprice_move
        self.timeout_s = timeout_s
        self.max_pending = max_pending
        self._clock = clock
        self._pending: list[_PendingMove] = []

    # -- inputs -----------------------------------------------------------

    def on_move(
        self,
        *,
        asset: str,
        move_pct: float,
        move_dir: str,
        token_id: str,
        baseline_mid: Optional[float],
        ts: Optional[float] = None,
    ) -> None:
        """Start measuring: a Binance move of |move_pct| was observed at `ts`
        (default: now). `token_id` is the direction-implied token and
        `baseline_mid` its mid at that moment. Skipped (not an error) when
        there is no usable baseline to compare against."""
        if baseline_mid is None or baseline_mid <= 0:
            return
        ts = self._clock() if ts is None else ts
        self._prune(ts)
        self._pending.append(
            _PendingMove(
                asset=asset, move_pct=move_pct, move_dir=move_dir,
                token_id=token_id, baseline_mid=baseline_mid,
                binance_move_ts=ts, expires_at=ts + self.timeout_s,
            )
        )
        if len(self._pending) > self.max_pending:
            del self._pending[: len(self._pending) - self.max_pending]

    def observe(
        self,
        *,
        token_id: str,
        mid: float,
        ts: Optional[float] = None,
    ) -> Optional[LagMeasurement]:
        """Feed a fresh Polymarket mid for a token. Finalizes (and removes)
        any pending move whose token has repriced by >= min_reprice_move.
        Returns the measurement, or None if nothing finalized."""
        ts = self._clock() if ts is None else ts
        finalized: Optional[LagMeasurement] = None
        remaining: list[_PendingMove] = []
        for p in self._pending:
            if p.token_id == token_id:
                change = mid - p.baseline_mid
                if abs(change) >= self.min_reprice_move:
                    finalized = LagMeasurement(
                        asset=p.asset, move_pct=p.move_pct, move_dir=p.move_dir,
                        token_id=p.token_id, binance_move_ts=p.binance_move_ts,
                        baseline_mid=p.baseline_mid, poly_repriced_ts=ts,
                        poly_move_pct=(change / p.baseline_mid) if p.baseline_mid else None,
                        timed_out=False, lag_ms=(ts - p.binance_move_ts) * 1000.0,
                    )
                    continue  # drop from pending — measured
            remaining.append(p)
        self._pending = remaining
        return finalized

    def sweep(self, ts: Optional[float] = None) -> list[LagMeasurement]:
        """Finalize anything past its timeout as timed_out (lag_ms=None).
        Returns the list of timed-out measurements."""
        ts = self._clock() if ts is None else ts
        timed_out: list[LagMeasurement] = []
        remaining: list[_PendingMove] = []
        for p in self._pending:
            if ts >= p.expires_at:
                timed_out.append(
                    LagMeasurement(
                        asset=p.asset, move_pct=p.move_pct, move_dir=p.move_dir,
                        token_id=p.token_id, binance_move_ts=p.binance_move_ts,
                        baseline_mid=p.baseline_mid, poly_repriced_ts=None,
                        poly_move_pct=None, timed_out=True, lag_ms=None,
                    )
                )
            else:
                remaining.append(p)
        self._pending = remaining
        return timed_out

    # -- introspection ----------------------------------------------------

    def pending_count(self) -> int:
        return len(self._pending)

    def pending_token_ids(self) -> set[str]:
        """Unique token_ids with at least one pending measurement — lets the
        caller feed `observe` only for tokens that actually have one."""
        return {p.token_id for p in self._pending}

    def _prune(self, ts: float) -> None:
        self._pending = [p for p in self._pending if p.expires_at > ts]
