"""
Feed health tracking for the two live market-data feeds.

Tracks, separately for the Binance feed and the Polymarket WebSocket feed:
- how many reconnects happened in the last RECONNECT_WINDOW_S (10 minutes)
- how long it's been since the last message was received

`is_healthy()` returns False if EITHER feed reconnected more than
MAX_RECONNECTS (3) times in the last 10 minutes, OR more than MAX_STALE_S
(10) seconds have passed since its last message. This is deliberately
conservative: a feed that's thrashing reconnects or gone silent means the
bot's inputs are unreliable, and trading on stale/absent market data is the
failure mode this is meant to prevent.

The class is pure in-memory state with an injectable clock — no I/O, no
network. Wiring it into the trading loop is the caller's job.
"""
from __future__ import annotations

import time
from typing import Callable

RECONNECT_WINDOW_S = 600.0     # 10 minutes
# The LAST HEALTHY count, not the first unhealthy one: the spec says "more
# than 3 reconnects in the window is unhealthy", so up to and including
# MAX_RECONNECTS (3) is healthy and 4+ is not.
MAX_RECONNECTS = 3
MAX_STALE_S = 10.0             # more than this many seconds since last message = unhealthy

FEEDS = ("binance", "polymarket")


class FeedHealth:
    """
    Per-feed health state: reconnect timestamps + last-message time.

    Usage (from the feed consumer in main.py):
        health = FeedHealth()
        # on every reconnect:    health.record_reconnect("binance")
        # on every message:      health.record_message("binance")
        # each trading cycle:    if not health.is_healthy(): skip trading

    An unknown feed name raises ValueError rather than silently tracking
    nothing — a typo'd feed is a bug, not a "no data" case.
    """

    def __init__(self, clock: Callable[[], float] = time.time):
        # feed -> list of reconnect timestamps (within the pruning window)
        self._reconnects: dict[str, list[float]] = {f: [] for f in FEEDS}
        # feed -> last message timestamp, or None if no message ever received
        self._last_message_at: dict[str, float | None] = {f: None for f in FEEDS}
        self._clock = clock

    def _require_feed(self, feed: str) -> None:
        if feed not in FEEDS:
            raise ValueError(f"unknown feed {feed!r}; expected one of {FEEDS}")

    def record_reconnect(self, feed: str) -> None:
        """Record that `feed` reconnected right now."""
        self._require_feed(feed)
        self._reconnects[feed].append(self._clock())

    def record_message(self, feed: str) -> None:
        """Record that `feed` delivered a message right now."""
        self._require_feed(feed)
        self._last_message_at[feed] = self._clock()

    def reconnect_count(self, feed: str) -> int:
        """
        Number of reconnects for `feed` within the last RECONNECT_WINDOW_S.
        Note: this mutates state, pruning entries older than the window as a
        side effect (they can never become relevant again, and pruning keeps
        the list bounded). is_healthy() calls this, so it also prunes.
        """
        self._require_feed(feed)
        cutoff = self._clock() - RECONNECT_WINDOW_S
        recent = [t for t in self._reconnects[feed] if t >= cutoff]
        self._reconnects[feed] = recent
        return len(recent)

    def seconds_since_last_message(self, feed: str) -> float | None:
        """
        Seconds since `feed` last delivered a message, or None if it has never
        delivered one. None is treated as unhealthy by is_healthy() — a feed
        that has never spoken is not a healthy feed.
        """
        self._require_feed(feed)
        last = self._last_message_at[feed]
        if last is None:
            return None
        return self._clock() - last

    def is_healthy(self) -> bool:
        """
        True only if BOTH feeds are healthy: each has reconnected at most
        MAX_RECONNECTS times in the last window AND received a message within
        the last MAX_STALE_S seconds (or is not stale if it never received
        one — a never-messaged feed is unhealthy).
        """
        for feed in FEEDS:
            if self.reconnect_count(feed) > MAX_RECONNECTS:
                return False
            since = self.seconds_since_last_message(feed)
            if since is None or since > MAX_STALE_S:
                return False
        return True
