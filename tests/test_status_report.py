"""Tests for the Telegram HTML formatters (alerts/status_report.py).

Regression: the /latency button was silent because keys that exist with
value None (latencies before any trade fires) crashed '{None:.0f}' inside
the handler, which swallows exceptions. These formatters must render
missing/None numbers as their defaults, never raise.
"""
from __future__ import annotations

from alerts.status_report import format_latency_html, format_risk_html


def test_latency_html_with_none_values_does_not_crash():
    """Latency dict where unmeasured timings are None (the live shape before
    any trade fires) must render defaults, not raise."""
    snap = {
        "latency": {
            "tick_to_signal_p50_ms": 364.4,
            "tick_to_signal_p95_ms": 1153.9,
            "tick_to_order_p50_ms": None,   # no trade fired yet
            "tick_to_order_p95_ms": None,   # no trade fired yet
            "platform_delay_ms": 250.0,
            "window_s": 2.0,
            "verdict": "n/a",
        }
    }
    html = format_latency_html(snap)
    assert "364 / 1154 ms" in html
    assert "0 / 0 ms" in html  # None -> default
    assert "verdict: n/a" in html


def test_latency_html_empty_handled():
    assert "no latency data" in format_latency_html({})
    assert "no latency data" in format_latency_html({"latency": {}})


def test_risk_html_with_none_percentages_does_not_crash():
    snap = {
        "risk_detail": {
            "daily_pnl_pct": None,               # never updated yet
            "daily_halt_threshold_pct": 0.20,
            "drawdown_pct": None,
            "kill_threshold_pct": 0.40,
        },
        "daily_halted": False,
        "kill_switch_tripped": False,
    }
    html = format_risk_html(snap)
    assert "halt at 20%" in html
    assert "kill at 40%" in html
