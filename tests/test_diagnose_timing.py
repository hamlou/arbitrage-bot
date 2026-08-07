"""
Tests for scripts/diagnose_timing.py's pure logic (verdict computation and
the structural budget), plus the report_latency window default. No DB access.
"""
import pytest

from scripts.diagnose_timing import _percentile, _verdict


def test_verdict_comfortable_when_well_under_half_window():
    assert _verdict(total_ms=400, window_ms=2000) == "comfortable"


def test_verdict_tight_when_under_window_but_above_half():
    assert _verdict(total_ms=1200, window_ms=2000) == "tight"


def test_verdict_too_slow_when_at_or_over_window():
    assert _verdict(total_ms=2000, window_ms=2000) == "too slow"
    assert _verdict(total_ms=2500, window_ms=2000) == "too slow"


def test_verdict_boundary_half_window():
    # Exactly half the window counts as tight (strict less-than for comfortable).
    assert _verdict(total_ms=1000, window_ms=2000) == "tight"


def test_percentile_basic():
    assert _percentile([1, 2, 3, 4], 0.5) == pytest.approx(2.5)
    assert _percentile([1, 2, 3, 4], 0.0) == pytest.approx(1.0)
    assert _percentile([1, 2, 3, 4], 1.0) == pytest.approx(4.0)


def test_percentile_empty_returns_nan():
    assert _percentile([], 0.5) != _percentile([], 0.5)  # NaN


def test_structural_budget_includes_platform_delay():
    """The 250ms CLOB taker delay is a fixed, non-engineerable component —
    it must be part of any honest timing budget, which the script adds to the
    measured p95."""
    from config.settings import settings

    assert settings.PLATFORM_TAKER_DELAY_MS == 250.0
    # A fast measured p95 (250ms) + platform delay (250ms) = 500ms, which
    # against a 2s window is comfortably inside.
    total = 250.0 + settings.PLATFORM_TAKER_DELAY_MS
    assert _verdict(total, settings.ASSUMED_ARBITRAGE_WINDOW_S * 1000) == "comfortable"
