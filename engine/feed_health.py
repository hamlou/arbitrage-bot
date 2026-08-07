"""
Feed health tracking for the two live market-data feeds.

Tracks, separately for the Binance feed and the Polymarket WebSocket feed:
- how many reconnects happened in the last RECONNECT_WINDOW_S (10 minutes) —
  a METRIC for dashboards/alerting, not the trading gate
- how long it's been since the last message was received

`is_healthy()` returns False if EITHER feed:
- has been silent for more than MAX_STALE_S (10) seconds, OR
- is in an active RECONNECT STORM: more than MAX_RECONNECTS (3) reconnects
  within the last RECONNECT_STORM_WINDOW_S (60) seconds.

Why quality-weighted instead of raw-reconnect-count (verified 2026-08-07):
the old gate flagged a feed UNHEALTHY for 4 reconnects spread over a 10-
minute window even while its books were 2ms fresh — halting trading for a
past burst that had already recovered. The data actually being read is the
thing that matters: if messages are current, the feed is usable; reconnects
are a warning metric, not a halt. Only a burst happening RIGHT NOW (60s
window) or actual staleness stops trading.

The class is pure in-memory state with an injectable clock — no I/O, no
network. Wiring it into the trading loop is the caller's job.
"""
from __future__ import annotations

import time
from typing import Callable

# Metric window: reconnects in the last 10 minutes, reported to the dashboard
# and status digests so reconnect trends are visible.
RECONNECT_WINDOW_S = 600.0
# GATE window: a burst of reconnects within this window is what actually
# halts trading. 60s — a short burst, not a 10-minute penalty box.
RECONNECT_STORM_WINDOW_S = 60.0
# The LAST HEALTHY count, not the first unhealthy one: the spec says "more
# than 3 reconnects in the window is unhealthy", so up to and including
# MAX_RECONNECTS (3) is healthy and 4+ is not.
MAX_RECONNECTS = 3
# Secondary gate (reviewed 2026-08-07): a feed reconnecting steadily (e.g.
# every ~90s) never clusters into a 60s storm, so the storm window alone
# would report it "healthy" indefinitely despite chronic instability. More
# than this many reconnects in the 10-minute METRIC window is unhealthy too.
# 6 = ~1.5/min sustained for 10 minutes.
MAX_RECONNECTS_10M = 6
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
        # feed -> list of reconnect timestamps within the 10-minute METRIC
        # window (dashboard/alerting) — see reconnect_count()
        self._reconnects: dict[str, list[float]] = {f: [] for f in FEEDS}
        # feed -> list of reconnect timestamps within the 60-second STORM
        # window (the actual trading gate) — see reconnect_storm_count()
        self._storm_reconnects: dict[str, list[float]] = {f: [] for f in FEEDS}
        # feed -> last message timestamp, or None if no message ever received
        self._last_message_at: dict[str, float | None] = {f: None for f in FEEDS}
        self._clock = clock

    def _require_feed(self, feed: str) -> None:
        if feed not in FEEDS:
            raise ValueError(f"unknown feed {feed!r}; expected one of {FEEDS}")

    def record_reconnect(self, feed: str) -> None:
        """Record that `feed` reconnected right now."""
        self._require_feed(feed)
        ts = self._clock()
        self._reconnects[feed].append(ts)
        # Opportunistically prune the storm list so a caller that never asks
        # for storm_count can't grow it unboundedly.
        storm_cutoff = ts - RECONNECT_STORM_WINDOW_S
        self._storm_reconnects[feed] = [
            t for t in self._storm_reconnects[feed] if t >= storm_cutoff
        ] + [ts]

    def record_message(self, feed: str) -> None:
        """Record that `feed` delivered a message right now."""
        self._require_feed(feed)
        self._last_message_at[feed] = self._clock()

    def reconnect_count(self, feed: str) -> int:
        """
        Number of reconnects for `feed` within the last RECONNECT_WINDOW_S
        (10 minutes). METRIC ONLY — not the trading gate. Note: this mutates
        state, pruning entries older than the window as a side effect (they
        can never become relevant again, and pruning keeps the list bounded).
        """
        self._require_feed(feed)
        cutoff = self._clock() - RECONNECT_WINDOW_S
        recent = [t for t in self._reconnects[feed] if t >= cutoff]
        self._reconnects[feed] = recent
        return len(recent)

    def reconnect_storm_count(self, feed: str) -> int:
        """
        Number of reconnects for `feed` within the last
        RECONNECT_STORM_WINDOW_S (60 seconds). THIS is the gate signal: a
        burst of reconnects happening right now means the connection is
        actively thrashing and its data is unreliable. Also prunes its own
        list (bounded).
        """
        self._require_feed(feed)
        cutoff = self._clock() - RECONNECT_STORM_WINDOW_S
        recent = [t for t in self._storm_reconnects[feed] if t >= cutoff]
        self._storm_reconnects[feed] = recent
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

    def is_feed_healthy(self, feed: str) -> bool:
        """
        True if THIS single feed has received a message within the last
        MAX_STALE_S seconds (a never-messaged feed is unhealthy) AND is not
        in an active reconnect storm (more than MAX_RECONNECTS reconnects in
        the last RECONNECT_STORM_WINDOW_S seconds) AND has not exceeded the
        sustained-reconnect hard limit (more than MAX_RECONNECTS_10M
        reconnects in the last 10 minutes). Reconnects that happened minutes
        ago and aged out of the storm window do NOT make a currently-fresh
        feed unhealthy — but a chronically unstable cadence still does.
        Exposed separately from is_healthy() so callers (e.g. the dashboard)
        can show per-feed status instead of only a combined bool.
        """
        self._require_feed(feed)
        if self.reconnect_storm_count(feed) > MAX_RECONNECTS:
            return False
        if self.reconnect_count(feed) > MAX_RECONNECTS_10M:
            return False
        since = self.seconds_since_last_message(feed)
        if since is None or since > MAX_STALE_S:
            return False
        return True

    def is_healthy(self) -> bool:
        """
        True only if BOTH feeds are healthy. See is_feed_healthy() for the
        per-feed definition.
        """
        return all(self.is_feed_healthy(feed) for feed in FEEDS)
