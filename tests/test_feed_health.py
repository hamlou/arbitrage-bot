"""
Tests for engine/feed_health.py — per-feed reconnect/staleness tracking and
the is_healthy() gate. Uses a controllable fake clock so no real time passes
and nothing touches the network.
"""
import pytest

from engine.feed_health import (
    FEEDS,
    MAX_RECONNECTS,
    MAX_STALE_S,
    RECONNECT_WINDOW_S,
    FeedHealth,
)


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def health(clock: FakeClock) -> FeedHealth:
    return FeedHealth(clock=clock)


def test_fresh_feeds_are_healthy(health, clock):
    for feed in FEEDS:
        health.record_message(feed)
    assert health.is_healthy() is True


def test_unknown_feed_raises(health):
    with pytest.raises(ValueError):
        health.record_message("not_a_feed")
    with pytest.raises(ValueError):
        health.reconnect_count("nope")


def test_stale_feed_is_unhealthy(health, clock):
    health.record_message("binance")
    health.record_message("polymarket")
    clock.advance(MAX_STALE_S + 0.1)
    assert health.is_healthy() is False


def test_never_messaged_feed_is_unhealthy(health):
    """A feed that has never delivered a message is not healthy."""
    health.record_message("binance")  # only one feed has spoken
    assert health.is_healthy() is False


def test_message_within_stale_limit_is_ok(health, clock):
    health.record_message("binance")
    health.record_message("polymarket")
    clock.advance(MAX_STALE_S)  # exactly at the limit is still healthy
    assert health.is_healthy() is True


def test_at_most_max_reconnects_is_healthy(health, clock):
    health.record_message("binance")
    health.record_message("polymarket")
    for _ in range(MAX_RECONNECTS):
        health.record_reconnect("binance")
    assert health.reconnect_count("binance") == MAX_RECONNECTS
    assert health.is_healthy() is True


def test_too_many_reconnects_is_unhealthy(health, clock):
    health.record_message("binance")
    health.record_message("polymarket")
    for _ in range(MAX_RECONNECTS + 1):
        health.record_reconnect("polymarket")
    assert health.is_healthy() is False


def test_old_reconnects_pruned_out_of_window(health, clock):
    health.record_message("binance")
    health.record_message("polymarket")
    for _ in range(MAX_RECONNECTS + 1):
        health.record_reconnect("binance")
    # All reconnects age out of the 10-minute window -> healthy again.
    clock.advance(RECONNECT_WINDOW_S + 1)
    assert health.reconnect_count("binance") == 0
    # But the feed has also gone stale now, so still unhealthy.
    assert health.is_healthy() is False

    # Fresh message after the old reconnects -> fully healthy.
    health.record_message("binance")
    health.record_message("polymarket")
    assert health.is_healthy() is True


def test_reconnect_count_prunes_older_entries(health, clock):
    health.record_reconnect("binance")          # t=0
    clock.advance(RECONNECT_WINDOW_S + 1)       # ages out
    health.record_reconnect("binance")          # t=window+1
    assert health.reconnect_count("binance") == 1


def test_seconds_since_last_message(health, clock):
    assert health.seconds_since_last_message("binance") is None
    health.record_message("binance")
    clock.advance(4.0)
    assert health.seconds_since_last_message("binance") == pytest.approx(4.0)


def test_is_feed_healthy_per_feed(health, clock):
    """is_feed_healthy() must report each feed independently, not the
    combined is_healthy() result — this is what the dashboard wiring
    relies on to show two separate indicators instead of one."""
    health.record_message("binance")
    # polymarket never messaged.
    assert health.is_feed_healthy("binance") is True
    assert health.is_feed_healthy("polymarket") is False
    assert health.is_healthy() is False  # combined still reflects the sick one


def test_is_feed_healthy_unknown_feed_raises(health):
    with pytest.raises(ValueError):
        health.is_feed_healthy("not_a_feed")


def test_health_tracks_feeds_independently(health, clock):
    """One sick feed must not be masked by a healthy one."""
    health.record_message("binance")
    health.record_message("polymarket")
    clock.advance(MAX_STALE_S + 5)  # both now stale
    health.record_message("polymarket")  # refresh ONLY polymarket
    assert health.is_healthy() is False   # binance is still stale

    health.record_message("binance")     # now both fresh
    assert health.is_healthy() is True
