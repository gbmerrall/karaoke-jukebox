"""Tests for queue behaviour against a real (migrated) database."""

import os
import time

import pytest


async def test_two_users_can_queue_same_video(initialized_db):
    """Distinct users may each queue the same video (the core feature)."""
    from app.services.queue_manager import queue_manager

    await queue_manager.add_to_queue("dQw4w9WgXcQ", "Song", "", 100, 1, "alice")
    await queue_manager.add_to_queue("dQw4w9WgXcQ", "Song", "", 100, 1, "bob")

    queue = await queue_manager.get_queue()
    assert len(queue) == 2
    assert {item["username"] for item in queue} == {"alice", "bob"}


async def test_same_user_cannot_duplicate(initialized_db):
    """A single user queueing the same active video twice is rejected."""
    from app.services.queue_manager import queue_manager

    await queue_manager.add_to_queue("dQw4w9WgXcQ", "Song", "", 100, 1, "alice")
    with pytest.raises(ValueError):
        await queue_manager.add_to_queue("dQw4w9WgXcQ", "Song", "", 100, 1, "alice")


async def test_cleanup_old_videos_respects_reference_and_age(initialized_db):
    """Only unreferenced files older than the threshold are deleted."""
    from app.config import settings
    from app.services.queue_manager import queue_manager

    videos_dir = settings.get_videos_dir()
    videos_dir.mkdir(parents=True, exist_ok=True)

    # Referenced by the queue -> must be kept regardless of age.
    await queue_manager.add_to_queue("dQw4w9WgXcQ", "Song", "", 100, 1, "alice")
    referenced = videos_dir / "dQw4w9WgXcQ.mp4"
    referenced.write_bytes(b"x")
    old_time = time.time() - 10 * 3600
    os.utime(referenced, (old_time, old_time))

    # Unreferenced and old -> deleted.
    stale = videos_dir / "staleVideo11.mp4"
    stale.write_bytes(b"x")
    os.utime(stale, (old_time, old_time))

    # Unreferenced but recent -> kept (might be an in-flight download).
    fresh = videos_dir / "freshVideo11.mp4"
    fresh.write_bytes(b"x")

    deleted = await queue_manager.cleanup_old_videos(hours_threshold=4)

    assert deleted == 1
    assert referenced.exists()
    assert fresh.exists()
    assert not stale.exists()
