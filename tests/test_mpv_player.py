"""Tests for MpvPlayer: outcome mapping, load-phase race, and idle screensaver.

No libmpv, no display. A FakeMpvModule/FakeMpvHandle pair stands in for
python-mpv: the handle records play/command calls and lets tests fire end-file
events on demand (simulating mpv's event thread). POLL_INTERVAL is shrunk so
the blocking play() loops spin fast; play() runs on a worker thread and tests
drive it via the fake handle.
"""

# standard library
import os
import threading
import time
from pathlib import Path

# 3rd party
import pytest

# project imports
import app.services.players.mpv_player as mpv_player
from app.config import settings
from app.services.players import PlaybackOutcome, Player
from app.services.players.mpv_player import MpvPlayer, _normalize_reason


class _FakeEvent:
    """Minimal python-mpv event: only as_dict() is used by the backend."""

    def __init__(self, data):
        self._data = data

    def as_dict(self):
        return self._data


class FakeMpvHandle:
    """Stands in for an mpv.MPV instance.

    play() records (path, loop_file-at-call) tuples; when auto_load is True it
    also sets self.path (mpv's `path` property) so the backend's load phase
    confirms immediately. fire_end_file() invokes the registered end-file
    callback the way mpv's event thread would.
    """

    def __init__(self, **options):
        self.options = options
        self.path = None
        self.loop_file = None
        self.play_calls = []  # list of (path, loop_file at time of call)
        self.commands = []
        self.terminated = False
        self.handlers = {}
        self.auto_load = True

    def event_callback(self, name):
        def decorator(fn):
            self.handlers[name] = fn
            return fn

        return decorator

    def play(self, path):
        self.play_calls.append((path, self.loop_file))
        if self.auto_load:
            self.path = path

    def command(self, *args):
        self.commands.append(args)
        if args and args[0] == "stop":
            self.path = None

    def terminate(self):
        self.terminated = True

    def fire_end_file(self, reason):
        """Deliver an end-file event to the backend (mpv event thread stand-in).

        Mirrors python-mpv >= 1.0's as_dict() shape: a FLAT map produced by
        libmpv's mpv_event_to_node, e.g. {"event": b"end-file", "reason":
        b"eof", "playlist_entry_id": N} - "event" is the event NAME, and the
        reason sits at the top level (verified against the python-mpv 1.0.8
        wheel, mpv.py:418). The nested {"event": {"reason": ...}} form belongs
        to the legacy 0.x API and is covered by its own dedicated test.
        """
        self.handlers["end-file"](
            _FakeEvent({"event": b"end-file", "reason": reason, "playlist_entry_id": 1})
        )


class FakeMpvModule:
    """Stands in for the `mpv` module: exposes MPV(**options)."""

    def __init__(self, init_error=None):
        self.init_error = init_error
        self.created = []

    def MPV(self, **options):
        if self.init_error is not None:
            raise self.init_error
        handle = FakeMpvHandle(**options)
        self.created.append(handle)
        return handle


@pytest.fixture
def make_backend(tmp_path, monkeypatch):
    """Factory building an MpvPlayer around a FakeMpvModule.

    Points settings.data_dir at tmp_path, shrinks POLL_INTERVAL, and calls
    startup(). Returns (player, handle); handle is None when init failed.
    Keyword args: idle='ok'|'missing'|'in-videos-dir'|None,
    init_error=<exception>.
    """
    created = []
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(mpv_player, "POLL_INTERVAL", 0.01)
    settings.get_videos_dir().mkdir(parents=True, exist_ok=True)

    def factory(idle="ok", init_error=None, relative_data_dir=False):
        if relative_data_dir:
            # Production's data_dir defaults to a relative "data" (not this
            # fixture's default absolute tmp_path), which is what actually
            # triggers mpv's absolute-path-expansion behavior below.
            monkeypatch.chdir(tmp_path)
            monkeypatch.setattr(settings, "data_dir", Path("data"))
            settings.get_videos_dir().mkdir(parents=True, exist_ok=True)
        if idle == "ok":
            idle_file = tmp_path / "idle.mp4"
            idle_file.write_bytes(b"fake idle video")
            monkeypatch.setattr(settings, "idle_video_path", idle_file)
        elif idle == "missing":
            monkeypatch.setattr(settings, "idle_video_path", tmp_path / "missing.mp4")
        elif idle == "in-videos-dir":
            idle_file = settings.get_videos_dir() / "idle.mp4"
            idle_file.write_bytes(b"fake idle video")
            monkeypatch.setattr(settings, "idle_video_path", idle_file)
        else:
            monkeypatch.setattr(settings, "idle_video_path", None)
        module = FakeMpvModule(init_error=init_error)
        player = MpvPlayer(mpv_module=module)
        player.startup()
        created.append(player)
        handle = module.created[0] if module.created else None
        return player, handle

    yield factory
    for player in created:
        player.shutdown()


def _add_video(video_id="vid1"):
    """Create a fake downloaded video file and return its path."""
    path = settings.get_video_path(video_id)
    path.write_bytes(b"fake mp4 bytes")
    return path


def _start_play(player, video_id="vid1"):
    """Run play() on a worker thread.

    Returns:
        (thread, result_dict, skip_event, stop_event); result_dict['outcome']
        holds the PlaybackOutcome once the thread finishes.
    """
    skip, stop = threading.Event(), threading.Event()
    result = {}

    def worker():
        result["outcome"] = player.play(video_id, skip, stop)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread, result, skip, stop


def _wait_until(condition, timeout=2.0):
    """Poll a condition to True within timeout (test helper)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return False


# ---------------------------------------------------------------------------
# Outcome mapping
# ---------------------------------------------------------------------------


def test_eof_returns_finished(make_backend):
    """A song that plays to its natural end maps to FINISHED."""
    player, handle = make_backend()
    _add_video()
    thread, result, _, _ = _start_play(player)
    assert player._load_confirmed.wait(2)
    handle.fire_end_file(b"eof")
    thread.join(timeout=2)
    assert result["outcome"] is PlaybackOutcome.FINISHED
    assert handle.play_calls[0][1] == "no"  # loop_file 'no' for songs


def test_legacy_nested_event_shape_returns_finished(make_backend):
    """Legacy python-mpv 0.x nests the payload: {"event": {"reason": <int>}}.

    The apt-packaged python3-mpv on Debian/Raspberry Pi OS is still the 0.x
    API family, so _on_end_file keeps a nested-dict fallback. python-mpv >= 1.0
    (the locked dependency) uses the flat shape exercised by fire_end_file and
    every other test in this file.
    """
    player, handle = make_backend()
    _add_video()
    thread, result, _, _ = _start_play(player)
    assert player._load_confirmed.wait(2)
    handle.handlers["end-file"](_FakeEvent({"event_id": 7, "event": {"reason": 0}}))
    thread.join(timeout=2)
    assert result["outcome"] is PlaybackOutcome.FINISHED


def test_skip_returns_skipped_and_clears_event(make_backend):
    """Skip stops mpv, clears the skip event, returns SKIPPED."""
    player, handle = make_backend()
    _add_video()
    thread, result, skip, _ = _start_play(player)
    assert player._load_confirmed.wait(2)
    skip.set()
    thread.join(timeout=2)
    assert result["outcome"] is PlaybackOutcome.SKIPPED
    assert not skip.is_set()
    assert ("stop",) in handle.commands


def test_stop_returns_stopped_and_keeps_event(make_backend):
    """Stop stops mpv, returns STOPPED, and never clears the stop event."""
    player, handle = make_backend()
    _add_video()
    thread, result, _, stop = _start_play(player)
    assert player._load_confirmed.wait(2)
    stop.set()
    thread.join(timeout=2)
    assert result["outcome"] is PlaybackOutcome.STOPPED
    assert stop.is_set()
    assert ("stop",) in handle.commands


def test_error_reason_returns_failed(make_backend):
    """An mpv error ending maps to FAILED (feeds the retry cap)."""
    player, handle = make_backend()
    _add_video()
    thread, result, _, _ = _start_play(player)
    assert player._load_confirmed.wait(2)
    handle.fire_end_file(b"error")
    thread.join(timeout=2)
    assert result["outcome"] is PlaybackOutcome.FAILED


def test_missing_file_fails_without_touching_screensaver(make_backend):
    """A missing video FAILS immediately and leaves idle state untouched."""
    player, handle = make_backend()
    timer_before = player._idle_timer
    outcome = player.play("no-such-video", threading.Event(), threading.Event())
    assert outcome is PlaybackOutcome.FAILED
    assert handle.play_calls == []  # mpv never asked to load anything
    assert player._idle_timer is timer_before  # startup's timer untouched


def test_duration_cap_returns_timed_out(make_backend, monkeypatch):
    """A song exceeding MAX_SONG_DURATION is stopped and TIMED_OUT."""
    monkeypatch.setattr(mpv_player, "MAX_SONG_DURATION", 0.05)
    player, handle = make_backend()
    _add_video()
    thread, result, _, _ = _start_play(player)
    thread.join(timeout=2)
    assert result["outcome"] is PlaybackOutcome.TIMED_OUT
    assert ("stop",) in handle.commands


def test_load_timeout_returns_failed(make_backend, monkeypatch):
    """mpv never confirming the load (corrupt/rejected file) maps to FAILED."""
    monkeypatch.setattr(mpv_player, "LOAD_TIMEOUT", 0.05)
    player, handle = make_backend()
    handle.auto_load = False  # mpv never reports the file as loaded
    _add_video()
    outcome = player.play("vid1", threading.Event(), threading.Event())
    assert outcome is PlaybackOutcome.FAILED
    assert ("stop",) in handle.commands


def test_play_when_unavailable_returns_failed(make_backend):
    """play() with no handle (startup failed) returns FAILED."""
    player, handle = make_backend(init_error=RuntimeError("no DRM device"))
    assert handle is None
    outcome = player.play("vid1", threading.Event(), threading.Event())
    assert outcome is PlaybackOutcome.FAILED


# ---------------------------------------------------------------------------
# Load-phase race (the replaced idle file's end event must be discarded)
# ---------------------------------------------------------------------------


def test_stale_end_event_from_replaced_idle_is_discarded(make_backend):
    """The idle file's end-file (fired during song load) must not end the song."""
    player, handle = make_backend()
    handle.auto_load = False  # we control exactly when the song 'loads'
    _add_video()
    thread, result, _, _ = _start_play(player)
    assert _wait_until(lambda: handle.play_calls)  # backend asked mpv to load
    handle.fire_end_file(b"stop")  # the replaced idle loop ending
    handle.path = handle.play_calls[-1][0]  # now the song is loaded
    assert player._load_confirmed.wait(2)
    handle.fire_end_file(b"eof")  # the song's real ending
    thread.join(timeout=2)
    assert result["outcome"] is PlaybackOutcome.FINISHED


def test_load_confirmed_when_mpv_reports_absolute_path(make_backend, monkeypatch):
    """mpv expands the loaded path to absolute; confirmation must still match.

    mpv's `path` property is documented to report the loaded file "expanded
    to an absolute path" (cwd prepended) - not the literal string passed to
    play(). Production requests a *relative* path (data_dir defaults to
    "data"), so a naive string comparison against the raw request never
    matches mpv's real report, and _wait_for_load times out even though
    playback is genuinely happening.
    """
    monkeypatch.setattr(mpv_player, "LOAD_TIMEOUT", 0.2)
    player, handle = make_backend(relative_data_dir=True)
    handle.auto_load = False  # we control exactly what mpv reports back
    _add_video()
    thread, result, _, _ = _start_play(player)
    assert _wait_until(lambda: handle.play_calls)
    requested_path, _ = handle.play_calls[-1]
    assert not os.path.isabs(requested_path), (
        "test setup should request a relative path"
    )
    handle.path = os.path.abspath(requested_path)  # mpv's real reporting behavior
    assert player._load_confirmed.wait(2)
    handle.fire_end_file(b"eof")
    thread.join(timeout=2)
    assert result["outcome"] is PlaybackOutcome.FINISHED


def test_skip_honored_during_load_phase(make_backend):
    """A skip that lands while the file is still loading is not lost."""
    player, handle = make_backend()
    handle.auto_load = False
    _add_video()
    thread, result, skip, _ = _start_play(player)
    assert _wait_until(lambda: handle.play_calls)
    skip.set()
    thread.join(timeout=2)
    assert result["outcome"] is PlaybackOutcome.SKIPPED
    assert not skip.is_set()


# ---------------------------------------------------------------------------
# Idle screensaver scheduling
# ---------------------------------------------------------------------------


def test_startup_arms_idle_timer(make_backend):
    """With a valid idle video, startup() arms the countdown."""
    player, _ = make_backend()
    assert player._idle_timer is not None


def test_no_idle_timer_when_unset(make_backend):
    """IDLE_VIDEO_PATH unset: screensaver disabled, no timer ever armed."""
    player, _ = make_backend(idle=None)
    assert player._idle_timer is None


def test_no_idle_timer_when_file_missing(make_backend):
    """A configured but missing idle file disables the screensaver."""
    player, _ = make_backend(idle="missing")
    assert player._idle_timer is None


def test_idle_video_inside_videos_dir_warns_but_works(make_backend, caplog):
    """An idle video in data/videos/ gets a loud warning at startup.

    The cleanup job deletes unreferenced .mp4 files from data/videos/, so the
    screensaver would silently vanish hours later. The screensaver still runs
    (cleanup may be disabled), but the admin is told at boot, not at midnight.
    """
    with caplog.at_level("WARNING", logger="app.services.players.mpv_player"):
        player, _ = make_backend(idle="in-videos-dir")
    assert player._idle_timer is not None  # still enabled
    assert any("cleanup job" in record.message for record in caplog.records), (
        "expected a startup warning about data/videos/ and the cleanup job"
    )


def test_play_cancels_idle_timer_and_rearms_after(make_backend):
    """The countdown is cancelled while a song plays and re-armed afterwards."""
    player, handle = make_backend()
    _add_video()
    thread, _, _, _ = _start_play(player)
    assert player._load_confirmed.wait(2)
    assert player._idle_timer is None  # cancelled for the duration of the song
    handle.fire_end_file(b"eof")
    thread.join(timeout=2)
    assert player._idle_timer is not None  # re-armed on the way out


def test_idle_timer_fires_and_starts_screensaver(make_backend, monkeypatch):
    """When the countdown elapses, the idle video plays with loop_file=inf."""
    monkeypatch.setattr(mpv_player, "IDLE_DELAY", 0.05)
    player, handle = make_backend()
    player._arm_idle_timer()  # re-arm with the shrunken delay
    assert _wait_until(lambda: handle.play_calls)
    idle_path, loop_at_call = handle.play_calls[-1]
    assert idle_path == str(settings.idle_video_path)
    assert loop_at_call == "inf"


def test_idle_start_yields_to_starting_song(make_backend):
    """A timer that fires just as a song starts must not steal the screen."""
    player, handle = make_backend()
    with player._state_lock:
        player._song_in_progress = True
    player._start_idle()
    assert handle.play_calls == []


def test_cleanup_stops_playback_keeps_handle_and_rearms(make_backend):
    """End of a playout session: stop the file, keep mpv, restart the countdown."""
    player, handle = make_backend()
    player.cleanup()
    assert ("stop",) in handle.commands
    assert handle.terminated is False
    assert player._idle_timer is not None


def test_shutdown_cancels_timer_and_terminates(make_backend):
    """App exit: countdown cancelled, handle terminated."""
    player, handle = make_backend()
    player.shutdown()
    assert player._idle_timer is None
    assert handle.terminated is True


# ---------------------------------------------------------------------------
# Availability and protocol conformance
# ---------------------------------------------------------------------------


def test_startup_failure_leaves_player_unavailable(make_backend):
    """An mpv init failure is logged, not raised; connect() then refuses."""
    player, handle = make_backend(init_error=RuntimeError("libmpv not found"))
    assert handle is None
    assert player.connect() is False


def test_connect_true_after_successful_startup(make_backend):
    """connect() is a cheap availability check once startup() succeeded."""
    player, _ = make_backend()
    assert player.connect() is True


async def test_discovery_stubs(make_backend):
    """mpv has no discoverable devices: stubs return empty/False."""
    player, _ = make_backend()
    assert player.supports_discovery is False
    assert player.selected_device_uuid is None
    assert await player.discover_devices() == []
    assert player.select_device("anything") is False


def test_satisfies_player_protocol():
    """MpvPlayer structurally satisfies the Player protocol."""
    assert isinstance(MpvPlayer(mpv_module=FakeMpvModule()), Player)


# ---------------------------------------------------------------------------
# end-file reason normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"eof", "eof"),
        ("eof", "eof"),
        ("STOP", "stop"),
        (b"error", "error"),
        (0, "eof"),
        (4, "error"),
        (None, "none"),
    ],
)
def test_normalize_reason_handles_all_forms(raw, expected):
    """Reasons arrive as bytes, str, or int codes depending on versions."""
    assert _normalize_reason(raw) == expected


# ---------------------------------------------------------------------------
# Video output enumeration
# ---------------------------------------------------------------------------


def _make_drm_entry(base: Path, name: str, status: str) -> None:
    """Create a fake /sys/class/drm/<name>/status file under base."""
    entry = base / name
    entry.mkdir(parents=True)
    (entry / "status").write_text(status)


def test_list_video_outputs_returns_connected_connectors(tmp_path):
    """Only 'connected' entries are returned, parsed into device+connector."""
    _make_drm_entry(tmp_path, "card0-HDMI-A-1", "disconnected")
    _make_drm_entry(tmp_path, "card1-HDMI-A-2", "connected")
    player = MpvPlayer(mpv_module=FakeMpvModule(), drm_base_path=tmp_path)
    outputs = player.list_video_outputs()
    assert outputs == [
        {
            "drm_device": "/dev/dri/card1",
            "drm_connector": "HDMI-A-2",
            "label": "HDMI-A-2 (card1)",
        }
    ]


def test_list_video_outputs_empty_when_sysfs_missing(tmp_path):
    """A missing sysfs tree (e.g. running off the Pi) yields no outputs."""
    player = MpvPlayer(mpv_module=FakeMpvModule(), drm_base_path=tmp_path / "missing")
    assert player.list_video_outputs() == []


def test_list_video_outputs_ignores_unreadable_status(tmp_path):
    """An entry with no readable status file is skipped, not fatal."""
    entry = tmp_path / "card2-HDMI-A-1"
    entry.mkdir(parents=True)
    # No status file at all - read_text() raises FileNotFoundError (an OSError).
    player = MpvPlayer(mpv_module=FakeMpvModule(), drm_base_path=tmp_path)
    assert player.list_video_outputs() == []


# ---------------------------------------------------------------------------
# Audio output enumeration
# ---------------------------------------------------------------------------


def test_list_audio_outputs_returns_mpv_device_list(make_backend):
    """Audio outputs come straight from mpv's own audio_device_list property."""
    player, handle = make_backend()
    handle.audio_device_list = [
        {"name": "alsa/sysdefault:CARD=iBassoDCSeries", "description": "USB DAC"},
        {"name": "auto", "description": "Autoselect device"},
    ]
    assert player.list_audio_outputs() == [
        {"name": "alsa/sysdefault:CARD=iBassoDCSeries", "description": "USB DAC"},
        {"name": "auto", "description": "Autoselect device"},
    ]


def test_list_audio_outputs_empty_when_unavailable(make_backend):
    """mpv unavailable (startup failed): no audio outputs to offer."""
    player, handle = make_backend(init_error=RuntimeError("no DRM device"))
    assert handle is None
    assert player.list_audio_outputs() == []


def test_list_audio_outputs_handles_query_error(make_backend, monkeypatch, caplog):
    """A property access error is swallowed and logged, not raised."""
    player, handle = make_backend()

    def _raise(self):
        raise RuntimeError("mpv IPC error")

    monkeypatch.setattr(
        type(handle), "audio_device_list", property(_raise), raising=False
    )
    with caplog.at_level("WARNING", logger="app.services.players.mpv_player"):
        assert player.list_audio_outputs() == []
    assert any("audio device list" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Output selection (video/audio switch)
# ---------------------------------------------------------------------------


def test_select_output_rejected_during_playback(make_backend):
    """A song in progress blocks the switch and leaves the handle untouched."""
    player, handle = make_backend()
    _add_video()
    thread, result, _, stop = _start_play(player)
    assert player._load_confirmed.wait(2)
    ok, message = player.select_output("/dev/dri/card0", "HDMI-A-1", "auto")
    assert ok is False
    assert "playback" in message.lower()
    assert handle.terminated is False
    stop.set()
    thread.join(timeout=2)


def test_select_output_recreates_handle_when_idle(make_backend):
    """Selecting a new output while idle terminates the old handle and
    builds a new one with the overridden options."""
    player, handle = make_backend()
    ok, message = player.select_output("/dev/dri/card0", "HDMI-A-1", "auto")
    assert ok is True
    assert message == ""
    assert handle.terminated is True
    new_handle = player._player
    assert new_handle is not handle
    assert new_handle.options["drm_device"] == "/dev/dri/card0"
    assert new_handle.options["drm_connector"] == "HDMI-A-1"
    assert new_handle.options["audio_device"] == "auto"
    # Options carried over unchanged (e.g. vo) are still present.
    assert new_handle.options["vo"] == "drm"


def test_select_output_same_device_terminates_before_building(make_backend):
    """A same-drm_device switch (e.g. audio-only) terminates the old handle
    up front, since mpv holds DRM master on that card."""
    player, handle = make_backend()
    same_device = player._current_options["drm_device"]
    ok, message = player.select_output(same_device, "HDMI-A-1", "auto")
    assert ok is True
    assert handle.terminated is True
    assert player._player is not handle


def test_select_output_same_device_failure_leaves_player_none(make_backend):
    """A failed same-device build leaves self._player None (the old handle
    was already terminated before the rebuild attempt, since it had to
    release the DRM card first)."""
    player, handle = make_backend()
    same_device = player._current_options["drm_device"]
    player._mpv_module.init_error = RuntimeError("DRM device busy")
    ok, message = player.select_output(same_device, "HDMI-A-1", "auto")
    assert ok is False
    assert "initialize" in message.lower()
    assert handle.terminated is True
    assert player._player is None


def test_select_output_failure_keeps_old_handle(make_backend):
    """If the new handle fails to init, the old handle stays in place."""
    player, handle = make_backend()
    player._mpv_module.init_error = RuntimeError("DRM device busy")
    ok, message = player.select_output("/dev/dri/card0", "HDMI-A-1", "auto")
    assert ok is False
    assert "initialize" in message.lower()
    assert player._player is handle
    assert handle.terminated is False


def test_select_output_rearms_idle_timer_after_success(make_backend):
    """A successful switch re-arms the idle countdown on the new handle."""
    player, _ = make_backend()
    player.select_output("/dev/dri/card0", "HDMI-A-1", "auto")
    assert player._idle_timer is not None


def test_play_after_select_output_uses_the_new_handle(make_backend):
    """A play() call started after a switch operates on the new handle, not
    a stale reference to the terminated one."""
    player, old_handle = make_backend()
    player.select_output("/dev/dri/card0", "HDMI-A-1", "auto")
    new_handle = player._player
    _add_video()
    thread, result, _, _ = _start_play(player)
    assert player._load_confirmed.wait(2)
    assert new_handle.play_calls  # the new handle was asked to load the song
    assert old_handle.play_calls == []  # the old (terminated) handle was not
    new_handle.fire_end_file(b"eof")
    thread.join(timeout=2)
    assert result["outcome"] is PlaybackOutcome.FINISHED
