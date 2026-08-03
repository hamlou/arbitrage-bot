"""
Tests for ui/dashboard.py — the rich terminal dashboard. Rendering is pure
(string/rich-object construction, no I/O), so these tests only exercise
DashboardState defaults, the feed-health panel, and that render() wires the
panel in without raising. No console is ever opened.
"""
from rich.panel import Panel

from ui.dashboard import DashboardState, _feed_health_panel, render


def test_feed_health_fields_default_to_false():
    state = DashboardState()
    assert state.binance_feed_healthy is False
    assert state.polymarket_feed_healthy is False


def test_feed_health_panel_shows_healthy_when_both_up():
    state = DashboardState(binance_feed_healthy=True, polymarket_feed_healthy=True)
    panel = _feed_health_panel(state)
    assert isinstance(panel, Panel)
    text = panel.renderable.renderables  # the Group's children
    joined = " ".join(str(t) for t in text)
    assert "Binance: healthy" in joined
    assert "Polymarket: healthy" in joined
    assert "UNHEALTHY" not in joined
    assert panel.border_style == "green"


def test_feed_health_panel_marks_binance_unhealthy():
    state = DashboardState(binance_feed_healthy=False, polymarket_feed_healthy=True)
    panel = _feed_health_panel(state)
    joined = " ".join(str(t) for t in panel.renderable.renderables)
    assert "Binance: UNHEALTHY" in joined
    assert "Polymarket: healthy" in joined
    assert panel.border_style == "red"


def test_feed_health_panel_marks_polymarket_unhealthy():
    state = DashboardState(binance_feed_healthy=True, polymarket_feed_healthy=False)
    panel = _feed_health_panel(state)
    joined = " ".join(str(t) for t in panel.renderable.renderables)
    assert "Binance: healthy" in joined
    assert "Polymarket: UNHEALTHY" in joined
    assert panel.border_style == "red"


def test_feed_health_panel_marks_both_unhealthy():
    state = DashboardState(binance_feed_healthy=False, polymarket_feed_healthy=False)
    panel = _feed_health_panel(state)
    joined = " ".join(str(t) for t in panel.renderable.renderables)
    assert "Binance: UNHEALTHY" in joined
    assert "Polymarket: UNHEALTHY" in joined
    assert panel.border_style == "red"


def test_render_includes_feed_health_panel():
    state = DashboardState(binance_feed_healthy=True, polymarket_feed_healthy=True)
    layout = render(state)
    top = layout["top"]
    names = [c.name for c in top.children]
    assert "mode" in names
    assert "risk" in names
    assert "feed_health" in names
