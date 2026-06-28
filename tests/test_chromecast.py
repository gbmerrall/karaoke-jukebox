"""Tests for the Chromecast playback service.

These tests never touch a real Chromecast or the network. The hardware/network
seams (`_connect_to_device`, `discover_devices`'s zeroconf objects) and the
async DB bridges are patched or driven against a real in-process event loop, so
the background playout logic can be exercised deterministically.
"""

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest

from app.services.chromecast import (
    MAX_PLAYBACK_RETRIES,
    ChromecastService,
    DiscoveryListener,
    chromecast_service,
)


def _make_fake_cast(name="Living Room"):
    """Build a MagicMock cast whose media_controller is fully scriptable.

    The returned mock exposes the attributes `_playout_loop` reads: `name`,
    `is_idle`, `play_media`, and a `media_controller` with a `status`
    (player_state/idle_reason), a `session_active_event`, and `stop`.

    Args:
        name: Friendly name reported by the fake cast.

    Returns:
        A MagicMock configured as a connected Chromecast.
    """
    cast = MagicMock()
    cast.name = name
    cast.is_idle = True
    status = MagicMock()
    status.player_state = "IDLE"
    status.idle_reason = "FINISHED"
    cast.media_controller.status = status
    cast.media_controller.session_active_event.wait.return_value = True
    return cast


def _queue_then_stop(service, item):
    """Return a side_effect for `_get_queue_sync` that yields one song then stops.

    The first call returns `[item]`; the second call sets `stop_requested` and
    returns `[]`, guaranteeing the outer playout `while` loop terminates after a
    single song instead of spinning forever.

    Args:
        service: The ChromecastService whose stop event should be set.
        item: The single queue row dict to return on the first call.

    Returns:
        A zero-argument callable suitable for MagicMock.side_effect.
    """
    calls = {"n": 0}

    def side_effect():
        calls["n"] += 1
        if calls["n"] == 1:
            return [item]
        service.stop_requested.set()
        return []

    return side_effect


# ---------------------------------------------------------------------------
# select_device
# ---------------------------------------------------------------------------


def test_select_device_sets_uuid():
    """A non-empty uuid is accepted and stored even if not in the cache."""
    service = ChromecastService()
    assert service.select_device("abc-123") is True
    assert service.selected_device_uuid == "abc-123"


def test_select_device_empty_string_returns_false():
    """An empty uuid is rejected (no device exists and the string is falsy)."""
    service = ChromecastService()
    assert service.select_device("") is False
    assert service.selected_device_uuid is None


# ---------------------------------------------------------------------------
# start / stop / skip / shutdown control surface
# ---------------------------------------------------------------------------


def test_start_playback_requires_selected_device():
    """Starting without a selected device fails with a clear message."""
    service = ChromecastService()
    result = service.start_playback()
    assert result == {"success": False, "message": "No Chromecast device selected"}


def test_start_playback_spawns_thread_and_blocks_second_start():
    """A selected device starts the loop; a second start reports 'already active'."""
    service = ChromecastService()
    service.select_device("abc-123")
    with patch.object(service, "_playout_loop", MagicMock()):
        first = service.start_playback()
        assert first["success"] is True
        assert service.is_playing is True
        second = service.start_playback()
    assert second == {"success": False, "message": "Playback is already active"}
    # The stubbed loop never flips is_playing off, so join just to be tidy.
    if service.playout_thread:
        service.playout_thread.join(timeout=1)


def test_stop_playback_when_idle_returns_not_active():
    """Stopping while idle is a no-op with a message."""
    service = ChromecastService()
    assert service.stop_playback() == {
        "success": False,
        "message": "Playback is not active",
    }


def test_stop_playback_when_playing_sets_event():
    """Stopping while active flips state and raises the stop event."""
    service = ChromecastService()
    service.is_playing = True
    result = service.stop_playback()
    assert result["success"] is True
    assert service.is_playing is False
    assert service.stop_requested.is_set()


def test_skip_current_when_idle_returns_not_active():
    """Skipping while idle is a no-op with a message."""
    service = ChromecastService()
    assert service.skip_current() == {
        "success": False,
        "message": "Playback is not active",
    }


def test_skip_current_when_playing_sets_event():
    """Skipping while active raises the skip event."""
    service = ChromecastService()
    service.is_playing = True
    result = service.skip_current()
    assert result["success"] is True
    assert service.skip_requested.is_set()


def test_shutdown_joins_running_thread():
    """shutdown sets stop, clears is_playing, and joins a live thread."""
    service = ChromecastService()
    started = threading.Event()

    def worker():
        started.set()
        service.stop_requested.wait(timeout=5)

    thread = threading.Thread(target=worker, daemon=True)
    service.playout_thread = thread
    service.is_playing = True
    thread.start()
    started.wait(timeout=5)

    service.shutdown(timeout=5)

    assert service.is_playing is False
    assert service.stop_requested.is_set()
    assert not thread.is_alive()


def test_shutdown_with_no_thread_is_safe():
    """shutdown is a clean no-op when no playout thread was ever started."""
    service = ChromecastService()
    service.playout_thread = None
    service.shutdown(timeout=1)
    assert service.is_playing is False
    assert service.stop_requested.is_set()


# ---------------------------------------------------------------------------
# DiscoveryListener
# ---------------------------------------------------------------------------


def test_discovery_listener_remove_and_noops():
    """remove_cast deletes a known uuid; add/update are no-ops."""
    listener = DiscoveryListener()
    uuid = "uuid-1"
    listener.devices[uuid] = MagicMock()

    # No-ops, called purely for coverage.
    listener.add_cast(uuid, "service")
    listener.update_cast(uuid, "service")

    listener.remove_cast(uuid, "service", MagicMock())
    assert uuid not in listener.devices
    # Removing an unknown uuid must not raise.
    listener.remove_cast("missing", "service", MagicMock())


# ---------------------------------------------------------------------------
# discover_devices
# ---------------------------------------------------------------------------


async def test_discover_devices_returns_device_dicts():
    """The patched zeroconf path returns name/uuid dicts and cleans up."""
    service = ChromecastService()

    svc = MagicMock()
    svc.friendly_name = "Den Cast"
    svc.uuid = "uuid-xyz"

    fake_browser = MagicMock()
    fake_browser.services = {"k": svc}

    fake_aiozc = MagicMock()
    fake_aiozc.zeroconf = MagicMock()

    async def fake_close():
        return None

    fake_aiozc.async_close.side_effect = fake_close

    with (
        patch("app.services.chromecast.AsyncZeroconf", return_value=fake_aiozc),
        patch("app.services.chromecast.CastBrowser", return_value=fake_browser),
    ):
        devices = await service.discover_devices(timeout=0)

    assert devices == [{"name": "Den Cast", "uuid": "uuid-xyz"}]
    fake_browser.start_discovery.assert_called_once()
    fake_browser.stop_discovery.assert_called_once()
    fake_aiozc.async_close.assert_called_once()


async def test_discover_devices_disconnects_idle_connection():
    """An existing idle connection is quit and disconnected before scanning."""
    service = ChromecastService()

    existing = MagicMock()
    existing.name = "Old Cast"
    existing.is_idle = False  # not idle -> quit_app must be called
    service.connected_cast = existing
    service.is_playing = False

    fake_browser = MagicMock()
    fake_browser.services = {}
    fake_aiozc = MagicMock()
    fake_aiozc.zeroconf = MagicMock()

    async def fake_close():
        return None

    fake_aiozc.async_close.side_effect = fake_close

    with (
        patch("app.services.chromecast.AsyncZeroconf", return_value=fake_aiozc),
        patch("app.services.chromecast.CastBrowser", return_value=fake_browser),
    ):
        devices = await service.discover_devices(timeout=0)

    assert devices == []
    existing.quit_app.assert_called_once()
    existing.disconnect.assert_called_once()
    assert service.connected_cast is None


async def test_discover_devices_returns_empty_on_exception():
    """If the browser construction raises, discovery returns an empty list."""
    service = ChromecastService()
    with (
        patch("app.services.chromecast.AsyncZeroconf", return_value=MagicMock()),
        patch(
            "app.services.chromecast.CastBrowser",
            side_effect=RuntimeError("boom"),
        ),
    ):
        devices = await service.discover_devices(timeout=0)
    assert devices == []


# ---------------------------------------------------------------------------
# Async DB bridges driven against a real running loop
# ---------------------------------------------------------------------------


async def _insert_song(username="alice"):
    """Insert one queued song and return its row id.

    Args:
        username: Owner of the queue row.

    Returns:
        The integer primary key of the inserted row.
    """
    from app.services.queue_manager import queue_manager

    await queue_manager.add_to_queue("dQw4w9WgXcQ", "Song", "", 100, 1, username)
    queue = await queue_manager.get_queue()
    return queue[0]["id"]


async def test_sync_bridges_against_real_loop(initialized_db):
    """The threadsafe bridges read/update/delete rows on the main loop."""
    service = ChromecastService()
    loop = asyncio.get_running_loop()
    service.set_event_loop(loop)

    qid = await _insert_song()

    # _get_queue_sync must run from a DIFFERENT thread than the loop.
    rows = await asyncio.to_thread(service._get_queue_sync)
    assert any(row["id"] == qid for row in rows)
    assert rows[0]["video_id"] == "dQw4w9WgXcQ"

    # Mark completed -> excluded from the active queue selection.
    await asyncio.to_thread(service._update_status_sync, qid, "completed")
    rows_after = await asyncio.to_thread(service._get_queue_sync)
    assert all(row["id"] != qid for row in rows_after)

    # Removal deletes the row entirely.
    await asyncio.to_thread(service._remove_from_queue_sync, qid)
    from app.services.queue_manager import queue_manager

    remaining = await queue_manager.get_queue()
    assert all(item["id"] != qid for item in remaining)


def test_get_queue_sync_without_loop_raises():
    """The bridges refuse to run before set_event_loop has been called."""
    service = ChromecastService()
    service.main_loop = None
    with pytest.raises(RuntimeError):
        service._get_queue_sync()
    with pytest.raises(RuntimeError):
        service._remove_from_queue_sync(1)
    with pytest.raises(RuntimeError):
        service._update_status_sync(1, "queued")


# ---------------------------------------------------------------------------
# _playout_loop driven directly (no real thread, no real cast)
# ---------------------------------------------------------------------------


def _run_loop(service, cast):
    """Run `_playout_loop` once with all external seams patched.

    Returns the MagicMocks for the three DB bridges so tests can assert calls.

    Args:
        service: The ChromecastService under test.
        cast: The fake cast returned by `_connect_to_device`.

    Returns:
        Tuple of (get_queue_mock, update_status_mock, remove_mock).
    """
    with (
        patch.object(service, "_connect_to_device", return_value=cast),
        patch.object(service, "_update_status_sync", MagicMock()) as update_mock,
        patch.object(service, "_remove_from_queue_sync", MagicMock()) as remove_mock,
        patch("app.services.chromecast.time.sleep", MagicMock()),
    ):
        service._playout_loop()
    return update_mock, remove_mock


def _item(qid=1):
    """Return a minimal queue row dict as produced by _get_queue_sync."""
    return {"id": qid, "video_id": "dQw4w9WgXcQ", "title": "Song"}


def test_playout_connect_failure_stops_cleanly():
    """When _connect_to_device returns None the loop bails and clears state."""
    service = ChromecastService()
    service.is_playing = True
    with (
        patch.object(service, "_connect_to_device", return_value=None),
        patch.object(service, "_get_queue_sync", MagicMock(return_value=[])),
        patch("app.services.chromecast.time.sleep", MagicMock()),
    ):
        service._playout_loop()
    assert service.is_playing is False


def test_playout_finished_removes_item():
    """A FINISHED song is removed and its failure count cleared."""
    service = ChromecastService()
    cast = _make_fake_cast()
    cast.media_controller.status.player_state = "IDLE"
    cast.media_controller.status.idle_reason = "FINISHED"
    service._failure_counts[1] = 2

    with patch.object(
        service, "_get_queue_sync", side_effect=_queue_then_stop(service, _item(1))
    ):
        update_mock, remove_mock = _run_loop(service, cast)

    remove_mock.assert_called_once_with(1)
    assert 1 not in service._failure_counts
    # Status was set to 'playing' before play_media; never 'completed'/'queued'.
    update_mock.assert_called_once_with(1, "playing")


def test_playout_error_increments_then_caps():
    """Repeated ERROR results eventually mark the song 'completed' (cap)."""
    service = ChromecastService()
    cast = _make_fake_cast()
    cast.media_controller.status.player_state = "IDLE"
    cast.media_controller.status.idle_reason = "ERROR"

    last_update = None
    last_remove = None
    for _ in range(MAX_PLAYBACK_RETRIES):
        service.stop_requested.clear()
        with patch.object(
            service, "_get_queue_sync", side_effect=_queue_then_stop(service, _item(7))
        ):
            last_update, last_remove = _run_loop(service, cast)

    # Final iteration hits the cap: status -> completed, never removed.
    last_update.assert_any_call(7, "completed")
    last_remove.assert_not_called()
    assert 7 not in service._failure_counts


def test_playout_skip_removes_item():
    """A skip request during playback stops the cast and removes the item."""
    service = ChromecastService()
    cast = _make_fake_cast()
    service.skip_requested.set()

    with patch.object(
        service, "_get_queue_sync", side_effect=_queue_then_stop(service, _item(3))
    ):
        update_mock, remove_mock = _run_loop(service, cast)

    cast.media_controller.stop.assert_called()
    remove_mock.assert_called_once_with(3)
    update_mock.assert_called_once_with(3, "playing")


def test_playout_stop_during_playback_keeps_item():
    """A stop raised during playback keeps the item queued (not removed)."""
    service = ChromecastService()
    cast = _make_fake_cast()

    def wait_and_stop(timeout):
        service.stop_requested.set()
        return True

    cast.media_controller.session_active_event.wait.side_effect = wait_and_stop

    with patch.object(
        service, "_get_queue_sync", side_effect=_queue_then_stop(service, _item(5))
    ):
        update_mock, remove_mock = _run_loop(service, cast)

    remove_mock.assert_not_called()
    update_mock.assert_any_call(5, "queued")


def test_playout_session_not_started_continues():
    """If the media session never starts the song is skipped without removal."""
    service = ChromecastService()
    cast = _make_fake_cast()
    cast.media_controller.session_active_event.wait.return_value = False

    with patch.object(
        service, "_get_queue_sync", side_effect=_queue_then_stop(service, _item(9))
    ):
        update_mock, remove_mock = _run_loop(service, cast)

    remove_mock.assert_not_called()
    # Only the pre-playback 'playing' update happened.
    update_mock.assert_called_once_with(9, "playing")


def test_playout_max_duration_advances_queue():
    """Exceeding MAX_SONG_DURATION stops the cast and advances the queue."""
    service = ChromecastService()
    cast = _make_fake_cast()
    # Keep player in a non-terminal state so only the duration guard fires.
    cast.media_controller.status.player_state = "PLAYING"

    with (
        patch.object(
            service,
            "_get_queue_sync",
            side_effect=_queue_then_stop(service, _item(11)),
        ),
        patch("app.services.chromecast.time.monotonic", side_effect=[0.0, 10**9]),
    ):
        update_mock, remove_mock = _run_loop(service, cast)

    cast.media_controller.stop.assert_called()
    remove_mock.assert_called_once_with(11)


def test_singleton_instance_is_service():
    """The module exposes a ready-to-use singleton."""
    assert isinstance(chromecast_service, ChromecastService)
