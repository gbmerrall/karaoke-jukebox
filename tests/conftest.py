"""
Shared pytest configuration and fixtures.

IMPORTANT: This module sets dummy environment variables BEFORE any `app.*`
module is imported. `app/config.py` calls `load_settings()` at import time and
calls `sys.exit(1)` if ADMIN_PASSWORD / YOUTUBE_API_KEY / SECRET_KEY are
missing. pytest imports conftest.py before collecting test modules, so setting
these here guarantees the app can be imported even on a machine with no `.env`
file (e.g. a contributor's clean checkout). The download integration test does
not use the YouTube API, so dummy values are sufficient.
"""

import os
import socket

# Use setdefault so a real environment (or exported vars) still wins, but a
# bare checkout with no configuration can still import and run the tests.
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("YOUTUBE_API_KEY", "test-youtube-api-key")
os.environ.setdefault("SECRET_KEY", "test-secret-key-at-least-32-characters-long-xx")

import pytest


def pytest_addoption(parser):
    """Register the --run-integration flag that opts in to network tests."""
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run integration tests that hit the live network (yt-dlp / YouTube).",
    )


def pytest_collection_modifyitems(config, items):
    """Skip integration-marked tests unless --run-integration is given."""
    if config.getoption("--run-integration"):
        return
    skip_integration = pytest.mark.skip(
        reason="needs --run-integration (hits the live network)"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)


def has_internet(
    host: str = "www.youtube.com", port: int = 443, timeout: float = 5.0
) -> bool:
    """Return True if a TCP connection to `host:port` succeeds within `timeout`."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture
async def initialized_db(tmp_path, monkeypatch):
    """Point settings at a tmp data dir and create a fresh, migrated schema.

    Yields the tmp data directory so tests can inspect files (e.g. videos).
    """
    from app.config import settings
    from app.database import init_db

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    await init_db()
    return tmp_path
