"""Tests for PlayoutService: queue policy driven with a scripted FakePlayer.

No pychromecast, no network. The FakePlayer returns canned PlaybackOutcomes so
every branch of the fate table is exercised deterministically. The async DB
bridges are exercised against a real in-process event loop (same approach as
the old test_chromecast.py).
"""

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest

from app.services.players import PlaybackOutcome
from app.services.players.chromecast_player import ChromecastPlayer
from app.services.playout import MAX_PLAYBACK_RETRIES, PlayoutService, playout_service


class FakePlayer:
    """Scripted Player: pops one outcome per play() call."""

    supports_discovery = False

    def __init__(self, outcomes=(), connect_ok=True):
        """Args:
        outcomes: Iterable of PlaybackOutcome returned by successive play() calls.
        connect_ok: Value returned by connect().
        """
        self.outcomes = list(outcomes)
        self.connect_ok = connect_ok
        self.selected_device_uuid = "fake-uuid"
        self.cleaned_up = False
        self.played = []
        self.started_up = False
        self.shut_down = False

    def connect(self):
        return self.connect_ok

    def play(self, video_id, skip_event, stop_event):
        self.played.append(video_id)
        return self.outcomes.pop(0)

    def cleanup(self):
        self.cleaned_up = True

    def startup(self):
        self.started_up = True

    def shutdown(self):
        self.shut_down = True

    async def discover_devices(self, timeout=10, keep_connection=False):
        return [{"name": "Fake", "uuid": "fake-uuid"}]

    def select_device(self, device_uuid):
        self.selected_device_uuid = device_uuid
        return True


def _item(qid=1):
    """Return a minimal queue row dict as produced by _get_queue_sync."""
    return {"id": qid, "video_id": "dQw4w9WgXcQ", "title": "Song"}


def _queue_then_stop(service, item):
    """side_effect for _get_queue_sync: one song, then stop the loop.

    Args:
        service: PlayoutService whose stop event gets set on the second call.
        item: Queue row dict returned on the first call.

    Returns:
        Zero-argument callable for MagicMock.side_effect.
    """
    calls = {"n": 0}

    def side_effect():
        calls["n"] += 1
        if calls["n"] == 1:
            return [item]
        service.stop_requested.set()
        return []

    return side_effect


def _run_loop(service):
    """Run _playout_loop once with the DB bridges patched.

    Args:
        service: PlayoutService under test (queue side_effect already patched
            by the caller via patch.object on _get_queue_sync).

    Returns:
        Tuple of (update_status mock, remove mock).
    """
    with (
        patch.object(service, "_update_status_sync", MagicMock()) as update_mock,
        patch.object(service, "_remove_from_queue_sync", MagicMock()) as remove_mock,
        patch("app.services.playout.time.sleep", MagicMock()),
    ):
        service._playout_loop()
    return update_mock, remove_mock


def _run_one_song(outcome, qid=1, player=None):
    """Drive one song through the loop with the given outcome.

    Args:
        outcome: PlaybackOutcome the FakePlayer returns.
        qid: Queue row id.
        player: Optional pre-built FakePlayer.

    Returns:
        Tuple of (service, update mock, remove mock).
    """
    service = PlayoutService(player or FakePlayer([outcome]))
    with patch.object(
        service, "_get_queue_sync", side_effect=_queue_then_stop(service, _item(qid))
    ):
        update_mock, remove_mock = _run_loop(service)
    return service, update_mock, remove_mock


# ---------------------------------------------------------------------------
# Control surface (start / stop / skip / shutdown)
# ---------------------------------------------------------------------------


def test_start_playback_discovery_backend_requires_device():
    """A discovery backend with no device selected refuses to start."""
    player = FakePlayer()
    player.supports_discovery = True
    player.selected_device_uuid = None
    service = PlayoutService(player)
    result = service.start_playback()
    assert result == {"success": False, "message": "No playback device selected"}


def test_start_playback_discoveryless_backend_needs_no_device():
    """A backend without discovery (mpv) starts with no device selected."""
    player = FakePlayer()
    player.selected_device_uuid = None
    service = PlayoutService(player)
    with patch.object(service, "_playout_loop", MagicMock()):
        result = service.start_playback()
        assert result["success"] is True
    if service.playout_thread:
        service.playout_thread.join(timeout=1)


def test_startup_delegates_to_player():
    """PlayoutService.startup() acquires the backend's app-lifetime resources."""
    player = FakePlayer()
    service = PlayoutService(player)
    service.startup()
    assert player.started_up is True


def test_shutdown_releases_player():
    """shutdown() releases the backend after the playout thread is joined."""
    player = FakePlayer()
    service = PlayoutService(player)
    service.playout_thread = None
    service.shutdown(timeout=1)
    assert player.shut_down is True


def test_start_playback_spawns_thread_and_blocks_second_start():
    """A selected device starts the loop; a second start reports 'already active'."""
    service = PlayoutService(FakePlayer())
    with patch.object(service, "_playout_loop", MagicMock()):
        first = service.start_playback()
        assert first["success"] is True
        assert service.is_playing is True
        second = service.start_playback()
    assert second == {"success": False, "message": "Playback is already active"}
    if service.playout_thread:
        service.playout_thread.join(timeout=1)


def test_stop_playback_when_idle_returns_not_active():
    """Stopping while idle is a no-op with a message."""
    service = PlayoutService(FakePlayer())
    assert service.stop_playback() == {
        "success": False,
        "message": "Playback is not active",
    }


def test_stop_playback_when_playing_sets_event():
    """Stopping while active flips state and raises the stop event."""
    service = PlayoutService(FakePlayer())
    service.is_playing = True
    result = service.stop_playback()
    assert result["success"] is True
    assert service.is_playing is False
    assert service.stop_requested.is_set()


def test_skip_current_when_idle_returns_not_active():
    """Skipping while idle is a no-op with a message."""
    service = PlayoutService(FakePlayer())
    assert service.skip_current() == {
        "success": False,
        "message": "Playback is not active",
    }


def test_skip_current_when_playing_sets_event():
    """Skipping while active raises the skip event."""
    service = PlayoutService(FakePlayer())
    service.is_playing = True
    result = service.skip_current()
    assert result["success"] is True
    assert service.skip_requested.is_set()


def test_shutdown_joins_running_thread():
    """shutdown sets stop, clears is_playing, and joins a live thread."""
    service = PlayoutService(FakePlayer())
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
    service = PlayoutService(FakePlayer())
    service.playout_thread = None
    service.shutdown(timeout=1)
    assert service.is_playing is False
    assert service.stop_requested.is_set()


# ---------------------------------------------------------------------------
# Delegation to the player
# ---------------------------------------------------------------------------


def test_selected_device_uuid_passthrough():
    """The /admin/status field reads through to the player's selection."""
    player = FakePlayer()
    service = PlayoutService(player)
    assert service.selected_device_uuid == "fake-uuid"
    player.selected_device_uuid = "other"
    assert service.selected_device_uuid == "other"


def test_select_device_delegates():
    """select_device passes through to the player."""
    player = FakePlayer()
    service = PlayoutService(player)
    assert service.select_device("abc-123") is True
    assert player.selected_device_uuid == "abc-123"


async def test_discover_devices_delegates_with_playing_flag():
    """Discovery passes is_playing down as keep_connection."""
    player = FakePlayer()
    player.supports_discovery = True
    captured = {}

    async def fake_discover(timeout=10, keep_connection=False):
        captured["keep_connection"] = keep_connection
        return []

    player.discover_devices = fake_discover
    service = PlayoutService(player)
    service.is_playing = True
    await service.discover_devices(timeout=0)
    assert captured["keep_connection"] is True


async def test_discover_devices_without_capability_returns_empty():
    """Backends without discovery yield an empty device list."""
    player = FakePlayer()
    player.supports_discovery = False
    service = PlayoutService(player)
    assert await service.discover_devices(timeout=0) == []


# ---------------------------------------------------------------------------
# Fate table (one test per PlaybackOutcome)
# ---------------------------------------------------------------------------


def test_finished_removes_item_and_resets_failures():
    """FINISHED removes the row and clears its failure count."""
    player = FakePlayer([PlaybackOutcome.FINISHED])
    service = PlayoutService(player)
    service._failure_counts[1] = 2
    with patch.object(
        service, "_get_queue_sync", side_effect=_queue_then_stop(service, _item(1))
    ):
        update_mock, remove_mock = _run_loop(service)
    remove_mock.assert_called_once_with(1)
    assert 1 not in service._failure_counts
    update_mock.assert_called_once_with(1, "playing")
    assert player.played == ["dQw4w9WgXcQ"]


def test_skipped_removes_item():
    """SKIPPED removes the row."""
    _, update_mock, remove_mock = _run_one_song(PlaybackOutcome.SKIPPED, qid=3)
    remove_mock.assert_called_once_with(3)
    update_mock.assert_called_once_with(3, "playing")


def test_timed_out_removes_item():
    """TIMED_OUT removes the row (matches legacy max-duration behavior)."""
    _, update_mock, remove_mock = _run_one_song(PlaybackOutcome.TIMED_OUT, qid=11)
    remove_mock.assert_called_once_with(11)


def test_stopped_requeues_item_without_failure_count():
    """STOPPED requeues the row and does not count a failure."""
    service, update_mock, remove_mock = _run_one_song(PlaybackOutcome.STOPPED, qid=5)
    remove_mock.assert_not_called()
    update_mock.assert_any_call(5, "queued")
    assert service._failure_counts == {}


def test_failed_increments_then_requeues():
    """The first two failures requeue the item with a growing count."""
    service = PlayoutService(FakePlayer([PlaybackOutcome.FAILED]))
    with patch.object(
        service, "_get_queue_sync", side_effect=_queue_then_stop(service, _item(7))
    ):
        update_mock, remove_mock = _run_loop(service)
    remove_mock.assert_not_called()
    update_mock.assert_any_call(7, "queued")
    assert service._failure_counts[7] == 1


def test_failed_caps_at_max_retries_marks_completed():
    """The MAX_PLAYBACK_RETRIES-th failure marks the row completed."""
    player = FakePlayer([PlaybackOutcome.FAILED] * MAX_PLAYBACK_RETRIES)
    service = PlayoutService(player)
    last_update = None
    last_remove = None
    for _ in range(MAX_PLAYBACK_RETRIES):
        service.stop_requested.clear()
        with patch.object(
            service, "_get_queue_sync", side_effect=_queue_then_stop(service, _item(7))
        ):
            last_update, last_remove = _run_loop(service)
    last_update.assert_any_call(7, "completed")
    last_remove.assert_not_called()
    assert 7 not in service._failure_counts


def test_player_exception_is_treated_as_failed():
    """A player that raises inside play() counts as a failure, loop survives."""
    player = FakePlayer()
    player.play = MagicMock(side_effect=RuntimeError("backend blew up"))
    service = PlayoutService(player)
    with patch.object(
        service, "_get_queue_sync", side_effect=_queue_then_stop(service, _item(9))
    ):
        update_mock, remove_mock = _run_loop(service)
    remove_mock.assert_not_called()
    update_mock.assert_any_call(9, "queued")
    assert service._failure_counts[9] == 1


# ---------------------------------------------------------------------------
# Thread lifecycle inside the loop
# ---------------------------------------------------------------------------


def test_connect_failure_stops_cleanly_without_cleanup():
    """When connect() fails the loop bails, clears state, skips cleanup()."""
    player = FakePlayer(connect_ok=False)
    service = PlayoutService(player)
    service.is_playing = True
    with patch.object(service, "_get_queue_sync", MagicMock(return_value=[])):
        with patch("app.services.playout.time.sleep", MagicMock()):
            service._playout_loop()
    assert service.is_playing is False
    assert player.cleaned_up is False


def test_cleanup_runs_after_loop_exits():
    """cleanup() is invoked in the finally block after a normal stop."""
    service, _, _ = _run_one_song(PlaybackOutcome.FINISHED)
    assert service.player.cleaned_up is True
    assert service.is_playing is False


# ---------------------------------------------------------------------------
# Async DB bridges against a real running loop
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
    service = PlayoutService(FakePlayer())
    loop = asyncio.get_running_loop()
    service.set_event_loop(loop)

    qid = await _insert_song()

    rows = await asyncio.to_thread(service._get_queue_sync)
    assert any(row["id"] == qid for row in rows)
    assert rows[0]["video_id"] == "dQw4w9WgXcQ"

    await asyncio.to_thread(service._update_status_sync, qid, "completed")
    rows_after = await asyncio.to_thread(service._get_queue_sync)
    assert all(row["id"] != qid for row in rows_after)

    await asyncio.to_thread(service._remove_from_queue_sync, qid)
    from app.services.queue_manager import queue_manager

    remaining = await queue_manager.get_queue()
    assert all(item["id"] != qid for item in remaining)


def test_bridges_without_loop_raise():
    """The bridges refuse to run before set_event_loop has been called."""
    service = PlayoutService(FakePlayer())
    service.main_loop = None
    with pytest.raises(RuntimeError):
        service._get_queue_sync()
    with pytest.raises(RuntimeError):
        service._remove_from_queue_sync(1)
    with pytest.raises(RuntimeError):
        service._update_status_sync(1, "queued")


# ---------------------------------------------------------------------------
# Singleton wiring
# ---------------------------------------------------------------------------


def test_singleton_wires_chromecast_player():
    """The module singleton is a PlayoutService driving a ChromecastPlayer."""
    assert isinstance(playout_service, PlayoutService)
    assert isinstance(playout_service.player, ChromecastPlayer)
