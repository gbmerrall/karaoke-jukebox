"""Unit tests for the in-memory rate limiter."""

from app.rate_limit import RateLimiter


def test_allows_up_to_limit_then_blocks():
    """The first N events are allowed; the next is blocked."""
    limiter = RateLimiter(max_events=3, window_seconds=60)
    assert limiter.allow("k") is True
    assert limiter.allow("k") is True
    assert limiter.allow("k") is True
    assert limiter.allow("k") is False


def test_keys_are_independent():
    """Exhausting one key does not affect another."""
    limiter = RateLimiter(max_events=1, window_seconds=60)
    assert limiter.allow("a") is True
    assert limiter.allow("a") is False
    assert limiter.allow("b") is True


def test_reset_clears_key():
    """reset() lets a previously-blocked key through again."""
    limiter = RateLimiter(max_events=1, window_seconds=60)
    assert limiter.allow("k") is True
    assert limiter.allow("k") is False
    limiter.reset("k")
    assert limiter.allow("k") is True


def test_window_expiry(monkeypatch):
    """Events older than the window no longer count toward the limit."""
    import app.rate_limit as rl

    fake_now = {"t": 1000.0}
    monkeypatch.setattr(rl.time, "monotonic", lambda: fake_now["t"])

    limiter = RateLimiter(max_events=2, window_seconds=10)
    assert limiter.allow("k") is True
    assert limiter.allow("k") is True
    assert limiter.allow("k") is False

    # Advance past the window; the earlier events should age out.
    fake_now["t"] = 1011.0
    assert limiter.allow("k") is True
