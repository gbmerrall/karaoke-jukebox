"""
Simple in-memory rate limiter.

This is a single-process, fixed-window sliding limiter intended for a
self-hosted app running with a single worker. It is NOT shared across
processes - if you ever run multiple workers, move this to a shared backend
(e.g. Redis). For a home/LAN karaoke app a single process is the norm, so this
keeps things dependency-free and easy to reason about.
"""

import time
from collections import defaultdict, deque
from threading import Lock


class RateLimiter:
    """Allow at most `max_events` per `window_seconds` for each key."""

    def __init__(self, max_events: int, window_seconds: float):
        """Initialise the limiter.

        Args:
            max_events: Maximum number of allowed events within the window.
            window_seconds: Length of the sliding window in seconds.
        """
        self.max_events = max_events
        self.window_seconds = window_seconds
        self._events: dict[str, deque] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        """Record an event for `key` and report whether it is permitted.

        Uses a monotonic clock so it is immune to wall-clock adjustments.

        Args:
            key: Identifier to rate-limit on (e.g. client IP or username).

        Returns:
            True if the event is within the limit (and was recorded), False if
            the key has exceeded `max_events` within the current window.
        """
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.max_events:
                return False
            events.append(now)
            return True

    def reset(self, key: str) -> None:
        """Clear all recorded events for `key` (e.g. after a successful login)."""
        with self._lock:
            self._events.pop(key, None)
