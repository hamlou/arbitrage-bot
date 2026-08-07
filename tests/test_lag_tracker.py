"""
Tests for engine/lag_tracker.py — the empirical arbitrage-window measurement
(how long Polymarket takes to reprice after a Binance move). Pure state
machine with an injectable clock — no I/O, no network.
"""
import pytest

from engine.lag_tracker import LagTracker


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_on_move_requires_baseline():
    tracker = LagTracker(clock=FakeClock())
    tracker.on_move(asset="BTC", move_pct=0.001, move_dir="UP", token_id="yes", baseline_mid=None)
    tracker.on_move(asset="BTC", move_pct=0.001, move_dir="UP", token_id="yes", baseline_mid=0.0)
    assert tracker.pending_count() == 0


def test_observe_finalizes_with_measured_lag():
    clock = FakeClock()
    tracker = LagTracker(min_reprice_move=0.005, timeout_s=30.0, clock=clock)
    tracker.on_move(asset="BTC", move_pct=0.002, move_dir="UP", token_id="yes",
                    baseline_mid=0.50, ts=clock.now)
    assert tracker.pending_count() == 1

    clock.advance(0.4)
    m = tracker.observe(token_id="yes", mid=0.507, ts=clock.now)

    assert m is not None
    assert m.lag_ms == pytest.approx(400.0)
    assert m.timed_out is False
    assert m.move_dir == "UP"
    assert m.baseline_mid == pytest.approx(0.50)
    assert tracker.pending_count() == 0


def test_observe_ignores_sub_threshold_moves():
    clock = FakeClock()
    tracker = LagTracker(min_reprice_move=0.005, timeout_s=30.0, clock=clock)
    tracker.on_move(asset="BTC", move_pct=0.002, move_dir="UP", token_id="yes",
                    baseline_mid=0.50, ts=clock.now)
    clock.advance(0.4)

    assert tracker.observe(token_id="yes", mid=0.503, ts=clock.now) is None
    assert tracker.pending_count() == 1  # still waiting


def test_sweep_times_out_unrepriced_moves():
    clock = FakeClock()
    tracker = LagTracker(min_reprice_move=0.005, timeout_s=30.0, clock=clock)
    tracker.on_move(asset="BTC", move_pct=0.002, move_dir="DOWN", token_id="no",
                    baseline_mid=0.50, ts=clock.now)
    clock.advance(31.0)

    out = tracker.sweep(ts=clock.now)

    assert len(out) == 1
    assert out[0].timed_out is True
    assert out[0].lag_ms is None
    assert out[0].poly_repriced_ts is None
    assert tracker.pending_count() == 0


def test_wrong_direction_response_still_measured():
    """Any >= threshold response counts as 'the market responded' — the actual
    direction is recorded in poly_move_pct so wrong-way repricings are visible
    in the data instead of silently dropping the measurement."""
    clock = FakeClock()
    tracker = LagTracker(min_reprice_move=0.005, timeout_s=30.0, clock=clock)
    tracker.on_move(asset="BTC", move_pct=0.002, move_dir="UP", token_id="yes",
                    baseline_mid=0.50, ts=clock.now)
    clock.advance(0.3)

    m = tracker.observe(token_id="yes", mid=0.493, ts=clock.now)

    assert m is not None
    assert m.poly_move_pct == pytest.approx(-0.014)


def test_pending_token_ids():
    clock = FakeClock()
    tracker = LagTracker(clock=clock)
    tracker.on_move(asset="BTC", move_pct=0.001, move_dir="UP", token_id="yes1",
                    baseline_mid=0.5, ts=clock.now)
    tracker.on_move(asset="ETH", move_pct=0.001, move_dir="DOWN", token_id="no2",
                    baseline_mid=0.5, ts=clock.now)
    assert tracker.pending_token_ids() == {"yes1", "no2"}


def test_to_db_row_shape():
    clock = FakeClock()
    tracker = LagTracker(clock=clock)
    tracker.on_move(asset="BTC", move_pct=0.001, move_dir="UP", token_id="yes",
                    baseline_mid=0.50, ts=clock.now)
    clock.advance(0.25)
    m = tracker.observe(token_id="yes", mid=0.51, ts=clock.now)
    row = m.to_db_row()
    assert row["lag_ms"] == pytest.approx(250.0)
    assert row["timed_out"] == 0
    assert row["move_dir"] == "UP"
    assert row["token_id"] == "yes"
