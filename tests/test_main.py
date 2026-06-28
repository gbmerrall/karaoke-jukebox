"""Unit tests for app.main wiring: health check, root, and the cleanup job.

The TestClient is created WITHOUT a `with` block so the lifespan (scheduler,
chromecast, ffmpeg checks) never runs. Only the request/response path and the
standalone cleanup coroutine are exercised.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app, cleanup_old_queue_items

client = TestClient(app)


def test_health_check(monkeypatch):
    """/health reports status, queue size and playing state."""
    monkeypatch.setattr(
        main_module.queue_manager, "get_queue_size", AsyncMock(return_value=3)
    )

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["queue_size"] == 3
    assert isinstance(body["is_playing"], bool)


def test_root_redirect_to_login():
    """The root path renders the login page (auth.router owns '/')."""
    response = client.get("/", follow_redirects=False)
    # auth.router's GET '/' wins and renders the login page (200).
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_cleanup_removes_items_and_videos(monkeypatch):
    """The cleanup job logs both removed queue items and unreferenced videos."""
    monkeypatch.setattr(
        main_module.queue_manager, "cleanup_old_items", AsyncMock(return_value=2)
    )
    monkeypatch.setattr(
        main_module.queue_manager, "cleanup_old_videos", AsyncMock(return_value=5)
    )

    # Should complete without raising.
    await cleanup_old_queue_items()

    main_module.queue_manager.cleanup_old_items.assert_awaited_once()
    main_module.queue_manager.cleanup_old_videos.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_nothing_to_do(monkeypatch):
    """The cleanup job handles the zero-removed case without error."""
    monkeypatch.setattr(
        main_module.queue_manager, "cleanup_old_items", AsyncMock(return_value=0)
    )
    monkeypatch.setattr(
        main_module.queue_manager, "cleanup_old_videos", AsyncMock(return_value=0)
    )

    await cleanup_old_queue_items()


@pytest.mark.asyncio
async def test_cleanup_swallows_exceptions(monkeypatch):
    """An error inside the cleanup job is caught and does not propagate."""
    monkeypatch.setattr(
        main_module.queue_manager,
        "cleanup_old_items",
        AsyncMock(side_effect=RuntimeError("db down")),
    )
    monkeypatch.setattr(
        main_module.queue_manager, "cleanup_old_videos", AsyncMock(return_value=0)
    )

    # Must not raise.
    await cleanup_old_queue_items()
