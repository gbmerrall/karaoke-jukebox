"""High-coverage tests for QueueManager against a real temp SQLite database.

These tests use the `initialized_db` fixture (a fresh, migrated schema in a
tmp data dir) rather than mocks, so they exercise the real SQL and the real
Jinja template rendering used by the SSE broadcast path.
"""

import asyncio
import json
import os
import time

from datetime import datetime, timedelta, timezone

import pytest


def _fresh_manager():
    """Return a brand-new QueueManager with an empty connection list.

    Methods read the database via the module-level get_db(), which honours the
    settings.data_dir monkeypatch from the initialized_db fixture, so a fresh
    instance still talks to the per-test temp database.

    Returns:
        A new QueueManager instance with no SSE connections.
    """
    from app.services.queue_manager import QueueManager

    return QueueManager()


async def _insert_row(video_id, username, added_at, status="queued"):
    """Insert a queue row directly, bypassing add_to_queue's broadcast.

    Args:
        video_id: YouTube video ID to store.
        username: Owner of the row.
        added_at: ISO-8601 timestamp string for the added_at column.
        status: Row status ('queued', 'playing', 'completed').

    Returns:
        The integer primary key of the inserted row.
    """
    from app.database import get_db

    async with get_db() as db:
        cursor = await db.execute(
            "INSERT INTO queue (video_id, title, thumbnail_url, duration, views, "
            "username, added_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (video_id, "T", "", 100, 1, username, added_at, status),
        )
        await db.commit()
        return cursor.lastrowid


async def test_add_returns_queued_and_blocks_self_duplicate(initialized_db):
    """add_to_queue returns status 'queued'; same (video_id, user) is rejected."""
    qm = _fresh_manager()

    result = await qm.add_to_queue("vid1", "Song", "", 100, 1, "alice")
    assert result["status"] == "queued"
    assert result["video_id"] == "vid1"
    assert result["username"] == "alice"
    assert isinstance(result["id"], int)

    with pytest.raises(ValueError, match="already queued"):
        await qm.add_to_queue("vid1", "Song", "", 100, 1, "alice")


async def test_add_allows_different_user_same_video(initialized_db):
    """A different username may queue the same video_id."""
    qm = _fresh_manager()

    await qm.add_to_queue("vid1", "Song", "", 100, 1, "alice")
    await qm.add_to_queue("vid1", "Song", "", 100, 1, "bob")

    assert await qm.get_queue_size() == 2


async def test_add_enforces_max_queue_size(initialized_db, monkeypatch):
    """When max_queue_size is reached, add_to_queue raises 'Queue is full'."""
    from app.config import settings

    monkeypatch.setattr(settings, "max_queue_size", 1)
    qm = _fresh_manager()

    await qm.add_to_queue("vid1", "Song", "", 100, 1, "alice")
    with pytest.raises(ValueError, match="Queue is full"):
        await qm.add_to_queue("vid2", "Other", "", 100, 1, "bob")


async def test_get_queue_excludes_completed_and_is_ordered(initialized_db):
    """get_queue returns only non-completed rows ordered by added_at."""
    qm = _fresh_manager()

    base = datetime.now(timezone.utc)
    later = (base + timedelta(minutes=1)).isoformat()
    earlier = base.isoformat()
    await _insert_row("late", "alice", later)
    await _insert_row("early", "alice", earlier)
    await _insert_row("done", "alice", earlier, status="completed")

    queue = await qm.get_queue()
    assert [item["video_id"] for item in queue] == ["early", "late"]
    assert await qm.get_queue_size() == 2


async def test_get_currently_playing(initialized_db):
    """get_currently_playing returns the playing row, else None."""
    qm = _fresh_manager()

    assert await qm.get_currently_playing() is None

    row_id = await qm.add_to_queue("vid1", "Song", "", 100, 1, "alice")
    assert await qm.get_currently_playing() is None

    await qm.update_status(row_id["id"], "playing")
    playing = await qm.get_currently_playing()
    assert playing is not None
    assert playing["video_id"] == "vid1"
    assert playing["status"] == "playing"


async def test_update_status_true_and_false(initialized_db):
    """update_status returns True for an existing row, False for a missing id."""
    qm = _fresh_manager()

    row = await qm.add_to_queue("vid1", "Song", "", 100, 1, "alice")
    assert await qm.update_status(row["id"], "completed") is True
    # The completed row no longer appears in the active queue.
    assert await qm.get_queue_size() == 0

    assert await qm.update_status(999999, "playing") is False


async def test_remove_admin_and_owner_and_permission(initialized_db):
    """Admin removes any item; owner removes own; non-owner raises PermissionError."""
    qm = _fresh_manager()

    a = await qm.add_to_queue("vidA", "A", "", 100, 1, "alice")
    b = await qm.add_to_queue("vidB", "B", "", 100, 1, "bob")

    # Admin can remove anyone's item.
    assert await qm.remove_from_queue(a["id"], username="admin", is_admin=True) is True

    # Owner can remove their own item.
    c = await qm.add_to_queue("vidC", "C", "", 100, 1, "carol")
    assert await qm.remove_from_queue(c["id"], username="carol") is True

    # Non-owner, non-admin is rejected.
    with pytest.raises(PermissionError):
        await qm.remove_from_queue(b["id"], username="mallory")


async def test_remove_missing_returns_false(initialized_db):
    """Removing a non-existent id returns False (both ownership-checked paths)."""
    qm = _fresh_manager()

    # Ownership-checked path (non-admin) with a missing id.
    assert await qm.remove_from_queue(999999, username="alice") is False
    # Admin path with a missing id (skips ownership lookup, hits rowcount==0).
    assert await qm.remove_from_queue(999999, is_admin=True) is False


async def test_clear_queue(initialized_db):
    """clear_queue returns the deleted count and empties the table."""
    qm = _fresh_manager()

    await qm.add_to_queue("vid1", "Song", "", 100, 1, "alice")
    await qm.add_to_queue("vid2", "Song2", "", 100, 1, "bob")

    assert await qm.clear_queue() == 2
    assert await qm.get_queue_size() == 0
    # Clearing an empty queue is a no-op returning 0.
    assert await qm.clear_queue() == 0


async def test_cleanup_old_items(initialized_db):
    """cleanup_old_items deletes rows older than the threshold and keeps fresh ones."""
    qm = _fresh_manager()

    old_time = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    fresh_time = datetime.now(timezone.utc).isoformat()
    await _insert_row("stale", "alice", old_time)
    await _insert_row("fresh", "alice", fresh_time)

    removed = await qm.cleanup_old_items(hours_threshold=24)
    assert removed == 1

    remaining = await qm.get_queue()
    assert [item["video_id"] for item in remaining] == ["fresh"]


async def test_reset_orphaned_items(initialized_db):
    """reset_orphaned_items flips 'playing' rows back to 'queued'."""
    qm = _fresh_manager()

    row = await qm.add_to_queue("vid1", "Song", "", 100, 1, "alice")
    await qm.update_status(row["id"], "playing")

    count = await qm.reset_orphaned_items()
    assert count == 1

    playing = await qm.get_currently_playing()
    assert playing is None
    queue = await qm.get_queue()
    assert queue[0]["status"] == "queued"

    # Nothing to reset now.
    assert await qm.reset_orphaned_items() == 0


async def test_cleanup_old_videos_keeps_referenced_and_recent(initialized_db):
    """cleanup_old_videos deletes only unreferenced files older than the threshold."""
    from app.config import settings

    qm = _fresh_manager()
    videos_dir = settings.get_videos_dir()
    videos_dir.mkdir(parents=True, exist_ok=True)

    # Referenced by a queued row -> kept regardless of age.
    await qm.add_to_queue("keepRef0001", "Song", "", 100, 1, "alice")
    referenced = videos_dir / "keepRef0001.mp4"
    referenced.write_bytes(b"x")
    old = time.time() - 10 * 3600
    os.utime(referenced, (old, old))

    # Unreferenced + old -> deleted.
    stale = videos_dir / "staleVid0001.mp4"
    stale.write_bytes(b"x")
    os.utime(stale, (old, old))

    # Unreferenced + recent -> kept (age guard protects in-flight downloads).
    fresh = videos_dir / "freshVid0001.mp4"
    fresh.write_bytes(b"x")

    deleted = await qm.cleanup_old_videos(hours_threshold=4)
    assert deleted == 1
    assert referenced.exists()
    assert fresh.exists()
    assert not stale.exists()


async def test_cleanup_old_videos_missing_dir(initialized_db):
    """cleanup_old_videos returns 0 when the videos directory does not exist."""
    qm = _fresh_manager()
    assert await qm.cleanup_old_videos(hours_threshold=4) == 0


def test_format_sse_event_html():
    """is_html=True prefixes each line with 'data: ' and adds a blank terminator."""
    qm = _fresh_manager()

    event = qm._format_sse_event("queue-update", "line1\nline2", is_html=True)
    assert event == "event: queue-update\ndata: line1\ndata: line2\n\n"


def test_format_sse_event_json():
    """is_html=False JSON-encodes the payload onto a single data line."""
    qm = _fresh_manager()

    event = qm._format_sse_event("heartbeat", {"status": "ok"}, is_html=False)
    assert event.startswith("event: heartbeat\ndata: ")
    assert event.endswith("\n\n")
    payload = event.split("data: ", 1)[1].rstrip("\n")
    assert json.loads(payload) == {"status": "ok"}


async def test_subscribe_yields_initial_then_broadcast(initialized_db):
    """subscribe emits an initial queue-update, then delivers broadcast events."""
    qm = _fresh_manager()

    gen = qm.subscribe("alice", is_admin=False)
    initial = await gen.__anext__()
    assert initial.startswith("event: queue-update\n")
    assert len(qm._connections) == 1

    # Adding an item triggers broadcast_queue_update internally; the next pull
    # from the generator should return that queued event without blocking.
    await qm.add_to_queue("vid1", "Song", "", 100, 1, "alice")
    update = await gen.__anext__()
    assert update.startswith("event: queue-update\n")
    assert "vid1" in update or "Song" in update

    await gen.aclose()
    assert qm._connections == []


async def test_subscribe_admin_template_renders(initialized_db):
    """subscribe with is_admin=True renders the admin template without error."""
    qm = _fresh_manager()

    await qm.add_to_queue("vid1", "Song", "", 100, 1, "alice")
    gen = qm.subscribe("admin", is_admin=True)
    initial = await gen.__anext__()
    assert initial.startswith("event: queue-update\n")
    await gen.aclose()
    assert qm._connections == []


async def test_broadcast_no_connections_is_noop(initialized_db):
    """broadcast_queue_update returns immediately when there are no connections."""
    qm = _fresh_manager()
    # Should not raise and should not require a queue read.
    await qm.broadcast_queue_update()
    assert qm._connections == []


async def test_broadcast_prunes_dead_connection(initialized_db):
    """A connection whose bounded queue is full is pruned during broadcast."""
    qm = _fresh_manager()

    full_queue = asyncio.Queue(maxsize=1)
    full_queue.put_nowait("already-full")
    dead_conn = {"queue": full_queue, "username": "ghost", "is_admin": False}
    qm._connections.append(dead_conn)

    await qm.add_to_queue("vid1", "Song", "", 100, 1, "alice")

    # put_nowait raised QueueFull, so the connection is removed.
    assert dead_conn not in qm._connections
    assert qm._connections == []
