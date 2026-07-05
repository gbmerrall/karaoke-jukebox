"""Tests for the ChromecastPlayer backend.

These tests never touch a real Chromecast or the network. The hardware seams
(_connect_to_device, zeroconf objects) are patched; play() is driven with a
scriptable fake cast object, mirroring the approach previously used in
tests/test_chromecast.py.
"""

import threading
from unittest.mock import MagicMock, patch

from app.services.players import PlaybackOutcome, Player
from app.services.players.chromecast_player import ChromecastPlayer, DiscoveryListener


def _make_fake_cast(name="Living Room"):
    """Build a MagicMock cast whose media_controller is fully scriptable.

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


def _connected_player(cast):
    """Return a ChromecastPlayer with the fake cast injected as connected.

    Args:
        cast: Fake cast object to install.

    Returns:
        A ChromecastPlayer ready for play() without any network activity.
    """
    player = ChromecastPlayer()
    player._cast = cast
    return player


def _events():
    """Return fresh (skip_event, stop_event) for a play() call."""
    return threading.Event(), threading.Event()


def test_satisfies_player_protocol():
    """ChromecastPlayer structurally satisfies the Player contract."""
    player = ChromecastPlayer()
    assert isinstance(player, Player)
    assert player.supports_discovery is True


class _CountingLock:
    """A context-manager that records how many times it was entered/exited.

    Used to prove the real threading.Lock in ChromecastPlayer is actually
    acquired during device-state access, not merely present as an attribute.
    """

    def __init__(self):
        self.enter_count = 0
        self.exit_count = 0

    def __enter__(self):
        self.enter_count += 1
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.exit_count += 1
        return False


# ---------------------------------------------------------------------------
# lock usage
# ---------------------------------------------------------------------------


def test_device_state_lock_is_exercised():
    """The shared lock is actually entered/exited around device-state access
    in select_device(), play()'s cast capture, and cleanup() - not merely
    present as an unused attribute.
    """
    cast = _make_fake_cast()
    player = _connected_player(cast)
    counting_lock = _CountingLock()
    player._lock = counting_lock

    assert player.select_device("abc-123") is True
    assert counting_lock.enter_count == 1
    assert counting_lock.exit_count == 1

    _play(player)
    assert counting_lock.enter_count == 2
    assert counting_lock.exit_count == 2

    player.cleanup()
    assert counting_lock.enter_count == 3
    assert counting_lock.exit_count == 3


# ---------------------------------------------------------------------------
# select_device
# ---------------------------------------------------------------------------


def test_select_device_sets_uuid():
    """A non-empty uuid is accepted and stored even if not in the cache."""
    player = ChromecastPlayer()
    assert player.select_device("abc-123") is True
    assert player.selected_device_uuid == "abc-123"


def test_select_device_empty_string_returns_false():
    """An empty uuid is rejected."""
    player = ChromecastPlayer()
    assert player.select_device("") is False
    assert player.selected_device_uuid is None


# ---------------------------------------------------------------------------
# connect / cleanup
# ---------------------------------------------------------------------------


def test_connect_without_selected_device_returns_false():
    """connect() refuses to run before a device has been selected."""
    player = ChromecastPlayer()
    assert player.connect() is False


def test_connect_failure_returns_false():
    """A device that cannot be reached yields False and no stored cast."""
    player = ChromecastPlayer()
    player.select_device("abc-123")
    with patch.object(player, "_connect_to_device", return_value=None):
        assert player.connect() is False
    assert player._cast is None


def test_connect_success_stores_cast():
    """A reachable device is stored for subsequent play() calls."""
    player = ChromecastPlayer()
    player.select_device("abc-123")
    cast = _make_fake_cast()
    with patch.object(player, "_connect_to_device", return_value=cast):
        assert player.connect() is True
    assert player._cast is cast


def test_cleanup_quits_and_disconnects():
    """cleanup() quits a non-idle app, disconnects, and clears the cast."""
    cast = _make_fake_cast()
    cast.is_idle = False
    player = _connected_player(cast)
    player.cleanup()
    cast.quit_app.assert_called_once()
    cast.disconnect.assert_called_once()
    assert player._cast is None


def test_cleanup_swallows_errors():
    """A disconnect error is logged, not raised."""
    cast = _make_fake_cast()
    cast.disconnect.side_effect = RuntimeError("gone")
    player = _connected_player(cast)
    player.cleanup()  # must not raise
    assert player._cast is None


# ---------------------------------------------------------------------------
# play() outcome mapping (the heart of the extraction)
# ---------------------------------------------------------------------------


def _play(player, skip=None, stop=None):
    """Run play() with time.sleep patched out for speed.

    Args:
        player: Player under test.
        skip: Optional pre-made skip event.
        stop: Optional pre-made stop event.

    Returns:
        The PlaybackOutcome.
    """
    skip_event, stop_event = _events()
    with patch("app.services.players.chromecast_player.time.sleep", MagicMock()):
        return player.play("dQw4w9WgXcQ", skip or skip_event, stop or stop_event)


def test_play_without_connect_fails():
    """play() before connect() is a FAILED outcome, not a crash."""
    player = ChromecastPlayer()
    assert _play(player) is PlaybackOutcome.FAILED


def test_play_finished():
    """idle_reason FINISHED maps to FINISHED."""
    cast = _make_fake_cast()
    player = _connected_player(cast)
    assert _play(player) is PlaybackOutcome.FINISHED
    cast.play_media.assert_called_once()
    args, kwargs = cast.play_media.call_args
    assert args[1] == "video/mp4"
    assert kwargs["stream_type"] == "BUFFERED"


def test_play_error_maps_to_failed():
    """idle_reason ERROR maps to FAILED."""
    cast = _make_fake_cast()
    cast.media_controller.status.idle_reason = "ERROR"
    player = _connected_player(cast)
    assert _play(player) is PlaybackOutcome.FAILED


def test_play_other_idle_reason_maps_to_failed():
    """Unexpected idle reasons (e.g. CANCELLED) map to FAILED."""
    cast = _make_fake_cast()
    cast.media_controller.status.idle_reason = "CANCELLED"
    player = _connected_player(cast)
    assert _play(player) is PlaybackOutcome.FAILED


def test_play_unknown_state_maps_to_failed():
    """Player state UNKNOWN maps to FAILED."""
    cast = _make_fake_cast()
    cast.media_controller.status.player_state = "UNKNOWN"
    player = _connected_player(cast)
    assert _play(player) is PlaybackOutcome.FAILED


def test_play_skip_returns_skipped_and_clears_event():
    """A raised skip event stops the cast, is cleared, and maps to SKIPPED."""
    cast = _make_fake_cast()
    player = _connected_player(cast)
    skip_event, stop_event = _events()
    skip_event.set()
    with patch("app.services.players.chromecast_player.time.sleep", MagicMock()):
        outcome = player.play("dQw4w9WgXcQ", skip_event, stop_event)
    assert outcome is PlaybackOutcome.SKIPPED
    assert not skip_event.is_set()
    cast.media_controller.stop.assert_called()


def test_play_stop_returns_stopped_event_left_set():
    """A raised stop event stops the cast and maps to STOPPED, event kept set."""
    cast = _make_fake_cast()
    player = _connected_player(cast)
    skip_event, stop_event = _events()
    stop_event.set()
    with patch("app.services.players.chromecast_player.time.sleep", MagicMock()):
        outcome = player.play("dQw4w9WgXcQ", skip_event, stop_event)
    assert outcome is PlaybackOutcome.STOPPED
    assert stop_event.is_set()
    cast.media_controller.stop.assert_called()


def test_play_session_not_started_is_failed():
    """BUG FIX: a session that never starts is a counted failure, not a retry loop."""
    cast = _make_fake_cast()
    cast.media_controller.session_active_event.wait.return_value = False
    player = _connected_player(cast)
    assert _play(player) is PlaybackOutcome.FAILED


def test_play_max_duration_times_out():
    """Exceeding MAX_SONG_DURATION stops the cast and maps to TIMED_OUT."""
    cast = _make_fake_cast()
    cast.media_controller.status.player_state = "PLAYING"
    player = _connected_player(cast)
    skip_event, stop_event = _events()
    with (
        patch("app.services.players.chromecast_player.time.sleep", MagicMock()),
        patch(
            "app.services.players.chromecast_player.time.monotonic",
            side_effect=[0.0, 10**9],
        ),
    ):
        outcome = player.play("dQw4w9WgXcQ", skip_event, stop_event)
    assert outcome is PlaybackOutcome.TIMED_OUT
    cast.media_controller.stop.assert_called()


def test_play_exception_maps_to_failed():
    """An exception inside playback is caught and mapped to FAILED."""
    cast = _make_fake_cast()
    cast.play_media.side_effect = RuntimeError("boom")
    player = _connected_player(cast)
    assert _play(player) is PlaybackOutcome.FAILED


# ---------------------------------------------------------------------------
# DiscoveryListener
# ---------------------------------------------------------------------------


def test_discovery_listener_remove_and_noops():
    """remove_cast deletes a known uuid; add/update are no-ops."""
    listener = DiscoveryListener()
    uuid = "uuid-1"
    listener.devices[uuid] = MagicMock()
    listener.add_cast(uuid, "service")
    listener.update_cast(uuid, "service")
    listener.remove_cast(uuid, "service", MagicMock())
    assert uuid not in listener.devices
    listener.remove_cast("missing", "service", MagicMock())


# ---------------------------------------------------------------------------
# discover_devices
# ---------------------------------------------------------------------------


def _fake_zeroconf_env(services):
    """Return (patch_ctx_managers, browser, aiozc) for a scripted scan.

    Args:
        services: Dict for the fake browser's .services attribute.

    Returns:
        Tuple of (AsyncZeroconf patch, CastBrowser patch, browser, aiozc).
    """
    fake_browser = MagicMock()
    fake_browser.services = services
    fake_aiozc = MagicMock()
    fake_aiozc.zeroconf = MagicMock()

    async def fake_close():
        return None

    fake_aiozc.async_close.side_effect = fake_close
    zc_patch = patch(
        "app.services.players.chromecast_player.AsyncZeroconf",
        return_value=fake_aiozc,
    )
    browser_patch = patch(
        "app.services.players.chromecast_player.CastBrowser",
        return_value=fake_browser,
    )
    return zc_patch, browser_patch, fake_browser, fake_aiozc


async def test_discover_devices_returns_device_dicts():
    """The patched zeroconf path returns name/uuid dicts and cleans up."""
    player = ChromecastPlayer()
    svc = MagicMock()
    svc.friendly_name = "Den Cast"
    svc.uuid = "uuid-xyz"
    zc_patch, browser_patch, fake_browser, fake_aiozc = _fake_zeroconf_env({"k": svc})
    with zc_patch, browser_patch:
        devices = await player.discover_devices(timeout=0)
    assert devices == [{"name": "Den Cast", "uuid": "uuid-xyz"}]
    fake_browser.start_discovery.assert_called_once()
    fake_browser.stop_discovery.assert_called_once()
    fake_aiozc.async_close.assert_called_once()


async def test_discover_devices_disconnects_idle_connection():
    """Without keep_connection, an existing cast is quit and disconnected first."""
    player = ChromecastPlayer()
    existing = MagicMock()
    existing.name = "Old Cast"
    existing.is_idle = False
    player._cast = existing
    zc_patch, browser_patch, _, _ = _fake_zeroconf_env({})
    with zc_patch, browser_patch:
        devices = await player.discover_devices(timeout=0, keep_connection=False)
    assert devices == []
    existing.quit_app.assert_called_once()
    existing.disconnect.assert_called_once()
    assert player._cast is None


async def test_discover_devices_keeps_active_connection():
    """With keep_connection=True the existing cast is left untouched."""
    player = ChromecastPlayer()
    existing = MagicMock()
    player._cast = existing
    zc_patch, browser_patch, _, _ = _fake_zeroconf_env({})
    with zc_patch, browser_patch:
        await player.discover_devices(timeout=0, keep_connection=True)
    existing.quit_app.assert_not_called()
    existing.disconnect.assert_not_called()
    assert player._cast is existing


async def test_discover_devices_returns_empty_on_exception():
    """If the browser construction raises, discovery returns an empty list."""
    player = ChromecastPlayer()
    with (
        patch(
            "app.services.players.chromecast_player.AsyncZeroconf",
            return_value=MagicMock(),
        ),
        patch(
            "app.services.players.chromecast_player.CastBrowser",
            side_effect=RuntimeError("boom"),
        ),
    ):
        devices = await player.discover_devices(timeout=0)
    assert devices == []
