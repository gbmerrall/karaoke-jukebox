# MpvPlayer Backend + Idle Screensaver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an mpv/DRM playback backend (Raspberry Pi + projector) with an idle
"screensaver" loop, selected via a `PLAYER_BACKEND` env var, behind the existing
Player/PlayoutService abstraction.

**Architecture:** The `Player` protocol gains app-lifetime `startup()`/`shutdown()`
hooks (Chromecast: no-ops; mpv: create/terminate one persistent libmpv handle).
`MpvPlayer` implements the blocking `play()` contract with the same poll-loop skeleton
as `ChromecastPlayer`, plus a timer-driven idle screensaver that is armed only from
threads we control. A factory keyed on `settings.player_backend` builds the singleton's
backend. The Chromecast-shaped `start_playback` device guard is generalized.

**Tech Stack:** Python 3.13, FastAPI, python-mpv (optional extra; lazy import),
pytest, uv, ruff. Stdlib logging (this project predates the loguru preference).

**Spec:** `docs/superpowers/specs/2026-07-07-mpv-player-backend-design.md` — read it
before starting. It explains WHY for every decision below (especially the load-phase
race and the "callback records, never schedules" rule).

## Global Constraints

- Run all commands from the repository root; use `uv run ...` for everything.
- Tests MUST pass (`uv run pytest`) and lint MUST be clean
  (`uv run ruff check . && uv run ruff format .`) before every commit.
- stdlib `logging` (module-level `logger = logging.getLogger(__name__)`), matching the
  existing services. No emojis anywhere. No co-author lines in commits.
- Google-style docstrings on methods. Import groups: stdlib, 3rd party, project —
  alphabetical within groups.
- Exact strings matter: the new guard message is `"No playback device selected"`.
  Legacy messages not named in this plan must not change.
- The `mpv` module import must remain lazy (inside `MpvPlayer.startup()`), and
  `mpv_player.py` must never be imported unless `PLAYER_BACKEND=mpv` (factory) or a
  test imports it directly. The dev/test environment does NOT install python-mpv.
- New constants live where the spec puts them: `MPV_OPTIONS`, `IDLE_DELAY`,
  `POLL_INTERVAL`, `LOAD_TIMEOUT` in `app/services/players/mpv_player.py`.

---

### Task 1: Player lifecycle hooks + start_playback guard generalization

The protocol gains `startup()`/`shutdown()` ("app lifetime") alongside the existing
`connect()`/`cleanup()` ("per playout session"). `PlayoutService` delegates both and
stops requiring a selected device for backends without discovery.

**Files:**
- Modify: `app/services/players/__init__.py` (protocol methods)
- Modify: `app/services/players/chromecast_player.py` (no-op implementations)
- Modify: `app/services/playout.py:108-131` (guard), `:164-178` (shutdown), new `startup()`
- Test: `tests/test_players.py`, `tests/test_playout.py`

**Interfaces:**
- Consumes: existing `Player` protocol, `PlayoutService`.
- Produces: `Player.startup() -> None`, `Player.shutdown() -> None` (required protocol
  methods), `PlayoutService.startup() -> None`, `PlayoutService.shutdown()` now calls
  `self.player.shutdown()` after joining the thread. Guard: discovery-less backends
  start playback with `selected_device_uuid is None`; discovery backends without a
  selection get `{"success": False, "message": "No playback device selected"}`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_players.py`, add `startup`/`shutdown` to `_StubPlayer` (it must keep
satisfying the protocol once the protocol grows):

```python
class _StubPlayer:
    """Minimal non-discovery backend proving the protocol is satisfiable."""

    supports_discovery = False

    def __init__(self):
        self.selected_device_uuid: Optional[str] = None

    def startup(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def connect(self) -> bool:
        return True

    def play(
        self,
        video_id: str,
        skip_event: threading.Event,
        stop_event: threading.Event,
    ) -> PlaybackOutcome:
        return PlaybackOutcome.FINISHED

    def cleanup(self) -> None:
        return None

    async def discover_devices(
        self, timeout: int = 10, keep_connection: bool = False
    ) -> List[Dict]:
        return []

    def select_device(self, device_uuid: str) -> bool:
        return False
```

Append two new tests to `tests/test_players.py`:

```python
def test_protocol_requires_lifecycle_methods():
    """A backend without startup/shutdown no longer satisfies the protocol."""

    class NoLifecycle:
        supports_discovery = False
        selected_device_uuid = None

        def connect(self) -> bool:
            return True

        def play(self, video_id, skip_event, stop_event) -> PlaybackOutcome:
            return PlaybackOutcome.FINISHED

        def cleanup(self) -> None:
            return None

        async def discover_devices(self, timeout=10, keep_connection=False):
            return []

        def select_device(self, device_uuid) -> bool:
            return False

    assert not isinstance(NoLifecycle(), Player)


def test_chromecast_player_satisfies_protocol():
    """The Chromecast backend implements the grown protocol (incl. lifecycle)."""
    from app.services.players.chromecast_player import ChromecastPlayer

    assert isinstance(ChromecastPlayer(), Player)
```

In `tests/test_playout.py`, extend `FakePlayer` with recording lifecycle methods
(insert after `cleanup`):

```python
    def startup(self):
        self.started_up = True

    def shutdown(self):
        self.shut_down = True
```

and add `self.started_up = False` / `self.shut_down = False` to `FakePlayer.__init__`.

REPLACE `test_start_playback_requires_selected_device` (test_playout.py:124-130)
with these two tests, and add the two delegation tests after them:

```python
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
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_players.py tests/test_playout.py -v`
Expected failures: `test_protocol_requires_lifecycle_methods` FAILS (protocol has no
lifecycle methods yet, so `NoLifecycle` still satisfies it),
`test_start_playback_discoveryless_backend_needs_no_device` FAILS (guard refuses),
`test_start_playback_discovery_backend_requires_device` FAILS (old message string),
`test_startup_delegates_to_player` FAILS (`AttributeError: startup`),
`test_shutdown_releases_player` FAILS (player.shut_down never set).

- [ ] **Step 3: Implement the contract and controller changes**

In `app/services/players/__init__.py`, inside the `Player` protocol, add these two
methods BEFORE `connect()`:

```python
    def startup(self) -> None:
        """Acquire app-lifetime resources. Called once from the app lifespan
        before any playback. Must not raise: implementations log failures and
        remember them so connect() returns False afterwards.
        """
        ...

    def shutdown(self) -> None:
        """Release app-lifetime resources. Called once at app exit, after the
        playout thread has been joined.
        """
        ...
```

Also update the protocol class docstring's second paragraph to mention the lifecycle
split (replace the existing docstring with):

```python
    """Contract between PlayoutService (queue policy) and a playback device.

    Lifecycle: startup()/shutdown() bracket the APP lifetime (persistent
    resources like mpv's handle); connect()/cleanup() bracket ONE playout
    session (the playout thread's connection). Backends without discoverable
    devices set supports_discovery = False and implement
    discover_devices/select_device as stubs returning [] / False.
    """
```

In `app/services/players/chromecast_player.py`, add no-ops to `ChromecastPlayer`
directly after `__init__`:

```python
    def startup(self) -> None:
        """No app-lifetime resources: the cast connection is per playout session."""
        return None

    def shutdown(self) -> None:
        """No app-lifetime resources: cleanup() already releases the session."""
        return None
```

In `app/services/playout.py`:

1. Add `startup()` right after `set_event_loop()`:

```python
    def startup(self) -> None:
        """Acquire the backend's app-lifetime resources (lifespan startup hook)."""
        self.player.startup()
```

2. Generalize the guard in `start_playback()` (currently lines 118-119):

```python
            if self.player.supports_discovery and not self.player.selected_device_uuid:
                return {"success": False, "message": "No playback device selected"}
```

3. Extend `shutdown()` to release the player after the join (the method becomes):

```python
    def shutdown(self, timeout: float = 10.0) -> None:
        """Stop playback, join the playout thread, and release the backend.

        Args:
            timeout: Seconds to wait for the playout thread to finish.
        """
        logger.info("Shutting down playout service")
        self.stop_requested.set()
        with self.playout_lock:
            self.is_playing = False
        thread = self.playout_thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning("Playout thread did not stop within timeout")
        # After the join so the playout thread cannot race the release.
        self.player.shutdown()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_players.py tests/test_playout.py -v`
Expected: ALL PASS (including the pre-existing tests — `test_shutdown_joins_running_thread`
and `test_shutdown_with_no_thread_is_safe` exercise shutdown() with FakePlayer, which
now has a `shutdown` method, so they still pass).

- [ ] **Step 5: Full suite + lint, then commit**

Run: `uv run pytest && uv run ruff check . && uv run ruff format .`
Expected: all tests pass (the suite was 207 passed, 1 skipped before this task;
count grows by the net new tests), no lint errors, no reformatting of untouched files.

```bash
git add app/services/players/__init__.py app/services/players/chromecast_player.py \
  app/services/playout.py tests/test_players.py tests/test_playout.py
git commit -m "feat: add app-lifetime player lifecycle hooks and generalize device guard"
```

---

### Task 2: Settings for backend selection and idle video

**Files:**
- Modify: `app/config.py` (two fields + two validators + one help line)
- Test: `tests/test_config_validation.py`

**Interfaces:**
- Consumes: existing `Settings` (pydantic-settings).
- Produces: `settings.player_backend: str` (normalized lowercase, `"chromecast"`
  default, only `"chromecast"`/`"mpv"` allowed) and
  `settings.idle_video_path: Optional[Path]` (`None` = screensaver disabled; empty
  env string coerces to `None`). Later tasks read exactly these two attributes.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config_validation.py`:

```python
def test_default_player_backend_is_chromecast():
    """Existing deployments keep Chromecast without setting anything."""
    assert _make().player_backend == "chromecast"


def test_player_backend_normalizes_case():
    """PLAYER_BACKEND=MPV works (normalized to lowercase)."""
    assert _make(player_backend="MPV").player_backend == "mpv"


def test_unknown_player_backend_rejected():
    """A typo in PLAYER_BACKEND fails startup instead of silently falling back."""
    with pytest.raises(ValidationError):
        _make(player_backend="vlc")


def test_idle_video_path_defaults_to_none():
    """No screensaver configured means None (disabled)."""
    assert _make().idle_video_path is None


def test_empty_idle_video_path_treated_as_unset():
    """IDLE_VIDEO_PATH= (blank) must not become Path('') / Path('.')."""
    assert _make(idle_video_path="   ").idle_video_path is None


def test_idle_video_path_parses_to_path():
    """A set value parses into a Path."""
    from pathlib import Path

    assert _make(idle_video_path="./data/idle.mp4").idle_video_path == Path(
        "data/idle.mp4"
    )
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_config_validation.py -v`
Expected: the six new tests FAIL (`player_backend`/`idle_video_path` don't exist —
note `extra="ignore"` means unknown kwargs are dropped, so the attribute accesses
raise `AttributeError`).

- [ ] **Step 3: Implement the settings**

In `app/config.py`:

1. Add `Optional` to the imports (`from typing import Optional` in the stdlib group).

2. Add the fields after the `# Server Configuration` pair (`server_host`/`server_port`):

```python
    # Playback backend: 'chromecast' (default) or 'mpv' (local video output).
    player_backend: str = "chromecast"
    # mpv only: video looped as an idle screensaver when nothing is playing.
    # None = disabled (black screen when idle).
    idle_video_path: Optional[Path] = None
```

3. Add validators after `validate_log_level`:

```python
    @field_validator("player_backend")
    @classmethod
    def validate_player_backend(cls, v: str) -> str:
        """Validate the playback backend selection (fail fast on typos)."""
        v_lower = v.strip().lower()
        if v_lower not in ("chromecast", "mpv"):
            raise ValueError(
                f"PLAYER_BACKEND must be 'chromecast' or 'mpv'. Got: {v}"
            )
        return v_lower

    @field_validator("idle_video_path", mode="before")
    @classmethod
    def empty_idle_video_path_is_none(cls, v):
        """Treat a blank IDLE_VIDEO_PATH as unset (screensaver disabled)."""
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return v
```

4. In `load_settings()`'s error help, add one line to the optional-variables block
(after the `SERVER_PORT` line):

```python
        logger.error(
            "  - PLAYER_BACKEND: 'chromecast' (default) or 'mpv' (local HDMI output)"
        )
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_config_validation.py tests/test_config_extra.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Full suite + lint, then commit**

Run: `uv run pytest && uv run ruff check . && uv run ruff format .`

```bash
git add app/config.py tests/test_config_validation.py
git commit -m "feat: add PLAYER_BACKEND and IDLE_VIDEO_PATH settings"
```

---

### Task 3: MpvPlayer backend with idle screensaver

The core of the feature. Read the spec's "MpvPlayer" section first — the load-phase
logic and the "callback records, never schedules" rule are both race fixes, not style.

**Files:**
- Create: `app/services/players/mpv_player.py`
- Create: `tests/test_mpv_player.py`

**Interfaces:**
- Consumes: `MAX_SONG_DURATION`, `PlaybackOutcome` from `app.services.players`;
  `settings.get_video_path(video_id)`, `settings.idle_video_path` from `app.config`.
- Produces: `MpvPlayer(mpv_module=None)` implementing the full `Player` protocol.
  `mpv_module` is a test seam: any object exposing `MPV(**options)`; `None` defers
  `import mpv` to `startup()`. Module constants `MPV_OPTIONS`, `IDLE_DELAY`,
  `POLL_INTERVAL`, `LOAD_TIMEOUT` (tests monkeypatch the latter three). Instance
  attribute `_load_confirmed: threading.Event` (set once `play()` has confirmed its
  file is loaded — tests synchronize on it).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mpv_player.py`:

```python
"""Tests for MpvPlayer: outcome mapping, load-phase race, and idle screensaver.

No libmpv, no display. A FakeMpvModule/FakeMpvHandle pair stands in for
python-mpv: the handle records play/command calls and lets tests fire end-file
events on demand (simulating mpv's event thread). POLL_INTERVAL is shrunk so
the blocking play() loops spin fast; play() runs on a worker thread and tests
drive it via the fake handle.
"""

# standard library
import threading
import time

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
        """Deliver an end-file event to the backend (mpv event thread stand-in)."""
        self.handlers["end-file"](_FakeEvent({"reason": reason}))


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
    Keyword args: idle='ok'|'missing'|None, init_error=<exception>.
    """
    created = []
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(mpv_player, "POLL_INTERVAL", 0.01)
    settings.get_videos_dir().mkdir(parents=True, exist_ok=True)

    def factory(idle="ok", init_error=None):
        if idle == "ok":
            idle_file = tmp_path / "idle.mp4"
            idle_file.write_bytes(b"fake idle video")
            monkeypatch.setattr(settings, "idle_video_path", idle_file)
        elif idle == "missing":
            monkeypatch.setattr(settings, "idle_video_path", tmp_path / "missing.mp4")
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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_mpv_player.py -v`
Expected: collection error — `ModuleNotFoundError: No module named
'app.services.players.mpv_player'`.

- [ ] **Step 3: Implement MpvPlayer**

Create `app/services/players/mpv_player.py`:

```python
"""
mpv playback backend: local video output (Raspberry Pi HDMI via DRM).

Device mechanics only: one persistent libmpv handle plays downloaded videos
and, when nothing has played for IDLE_DELAY seconds, an idle "screensaver"
loop. Queue policy lives in app/services/playout.py.

Threading and design rules (see the 2026-07-07 spec):
- The mpv handle is app-lifetime: created in startup(), destroyed in
  shutdown(). connect()/cleanup() are per-playout-session hooks and never
  touch the handle, so the screensaver survives playout sessions.
- The end-file callback (mpv's event thread) only RECORDS how playback ended;
  it never starts or schedules anything. Idle timers are armed exclusively
  from threads we control: startup(), play()'s exit, and cleanup().
- Loading a song over the screensaver makes mpv fire end-file for the IDLE
  file, and python-mpv end-file events do not reliably carry the filename
  across libmpv versions. play() therefore confirms via mpv's `path` property
  that its own file is the one loaded, then discards any end event recorded
  earlier (same family of problem as the Chromecast stale-status delay).
"""

# standard library
import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

# project imports
from app.config import settings
from app.services.players import MAX_SONG_DURATION, PlaybackOutcome

logger = logging.getLogger(__name__)

# Validated on the target Pi hardware with a command-line prototype. The admin
# customisation pass will replace these constants with persisted settings.
MPV_OPTIONS = {
    "vo": "drm",
    "drm_device": "/dev/dri/card1",
    "drm_connector": "HDMI-A-2",
    "drm_mode": "1280x720",
    "hwdec": "v4l2m2m",
    "idle": "yes",  # keep the handle alive with nothing loaded
}

IDLE_DELAY = 15.0  # seconds of nothing playing before the screensaver starts
POLL_INTERVAL = 0.2  # seconds between checks in the play() wait loops
LOAD_TIMEOUT = 10.0  # seconds for mpv to confirm a file loaded (else FAILED)

# libmpv numeric end-file reason codes (mpv_end_file_reason in client.h).
_REASON_CODES = {0: "eof", 2: "stop", 3: "quit", 4: "error", 5: "redirect"}


def _normalize_reason(reason) -> str:
    """Normalize an end-file reason to a lowercase string.

    python-mpv delivers the reason as bytes, str, or an int code depending on
    the libmpv + python-mpv version.

    Args:
        reason: Raw `reason` field from an end-file event dict.

    Returns:
        Lowercase string form, e.g. 'eof', 'stop', or 'error'.
    """
    if isinstance(reason, bytes):
        return reason.decode("utf-8", errors="replace").lower()
    if isinstance(reason, str):
        return reason.lower()
    if isinstance(reason, int):
        return _REASON_CODES.get(reason, "unknown")
    return str(reason).lower()


class MpvPlayer:
    """Player backend driving a local display through a persistent libmpv handle."""

    supports_discovery = False

    def __init__(self, mpv_module=None):
        """Initialize without touching libmpv (that happens in startup()).

        Args:
            mpv_module: Module-like object exposing an MPV class. None
                (production) defers `import mpv` to startup(); tests inject a
                fake module.
        """
        self.selected_device_uuid: Optional[str] = None
        self._mpv_module = mpv_module
        self._player = None  # persistent mpv.MPV handle, created in startup()
        self._idle_path: Optional[Path] = None

        # Shared between the playout thread, mpv's event thread, and idle
        # timer threads. _state_lock guards everything below it.
        self._state_lock = threading.Lock()
        self._idle_timer: Optional[threading.Timer] = None
        self._song_in_progress = False
        self._end_reason: Optional[str] = None  # last recorded end-file reason

        # Set once play() has confirmed its file is the one mpv loaded.
        # Exists for observability (tests synchronize on it); play() polls.
        self._load_confirmed = threading.Event()

    # ------------------------------------------------------------------
    # App-lifetime lifecycle
    # ------------------------------------------------------------------

    def startup(self) -> None:
        """Create the persistent mpv handle and arm the idle screensaver.

        Called once from the app lifespan. Never raises: on failure the
        player logs, stays unavailable, and connect() returns False so the
        playout loop aborts cleanly while the admin UI stays reachable.
        """
        try:
            mpv_module = self._mpv_module
            if mpv_module is None:
                # Deferred import: requires libmpv (install the 'mpv' extra).
                import mpv as mpv_module
            player = mpv_module.MPV(**MPV_OPTIONS)
            player.event_callback("end-file")(self._on_end_file)
        except Exception as e:
            logger.error(f"mpv initialization failed: {e}", exc_info=True)
            self._player = None
            return

        self._player = player
        logger.info(f"mpv initialized with options: {MPV_OPTIONS}")

        self._idle_path = self._resolve_idle_path()
        if self._idle_path is not None:
            self._arm_idle_timer()

    def shutdown(self) -> None:
        """Cancel timers and terminate the mpv handle. Called once at app exit."""
        with self._state_lock:
            self._cancel_idle_timer_locked()
            # Blocks a timer callback already past cancel() from starting the
            # screensaver on a dying handle.
            self._song_in_progress = True
        player = self._player
        self._player = None
        if player is None:
            return
        try:
            player.terminate()
            logger.info("mpv terminated")
        except Exception as e:
            logger.warning(f"Error terminating mpv: {e}")

    # ------------------------------------------------------------------
    # Per-playout-session lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Report whether the app-lifetime handle is usable.

        Returns:
            True when startup() produced a handle; False aborts the playout
            thread (libmpv missing, DRM init failed, or startup() never ran).
        """
        if self._player is None:
            logger.error("mpv is not available (startup failed or never ran)")
            return False
        return True

    def cleanup(self) -> None:
        """End a playout session: stop the current file, keep the handle.

        The screensaver must survive playout sessions, so this never destroys
        the handle; it stops playback and re-arms the idle countdown.
        """
        player = self._player
        if player is None:
            return
        self._stop_mpv(player)
        self._arm_idle_timer()

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------

    def play(
        self,
        video_id: str,
        skip_event: threading.Event,
        stop_event: threading.Event,
    ) -> PlaybackOutcome:
        """Play one downloaded video, blocking until there is an outcome.

        Args:
            video_id: YouTube id of a downloaded video in data/videos/.
            skip_event: Set by the controller to skip; cleared here when honored.
            stop_event: Set by the controller to stop; never cleared here.

        Returns:
            The PlaybackOutcome. All exceptions are caught and become FAILED.
        """
        player = self._player
        if player is None:
            logger.error("play() called but mpv is unavailable")
            return PlaybackOutcome.FAILED

        # Missing file: fail before touching idle state so a running
        # screensaver is not interrupted for a song we cannot play.
        video_path = settings.get_video_path(video_id)
        if not video_path.exists():
            logger.error(f"Video file missing: {video_path}")
            return PlaybackOutcome.FAILED

        self._load_confirmed.clear()
        with self._state_lock:
            # Flag first: a concurrent _start_idle() holding the lock finishes
            # its idle load before we proceed, and our song load then wins.
            self._song_in_progress = True
            self._cancel_idle_timer_locked()
            self._end_reason = None

        try:
            player.loop_file = "no"
            player.play(str(video_path))
            logger.info(f"mpv loading: {video_path}")

            load_outcome = self._wait_for_load(
                player, str(video_path), skip_event, stop_event
            )
            if load_outcome is not None:
                return load_outcome

            return self._monitor_playback(player, skip_event, stop_event)

        except Exception as e:
            logger.error(f"Error during mpv playback: {e}", exc_info=True)
            return PlaybackOutcome.FAILED

        finally:
            with self._state_lock:
                self._song_in_progress = False
            # Re-arm on every exit: the next play() cancels it within
            # INTER_SONG_PAUSE (1s) when more songs are queued, so the
            # screensaver only appears when playback genuinely stops.
            self._arm_idle_timer()

    def _wait_for_load(
        self,
        player,
        video_path: str,
        skip_event: threading.Event,
        stop_event: threading.Event,
    ) -> Optional[PlaybackOutcome]:
        """Wait until mpv confirms `video_path` is the loaded file.

        End events recorded before confirmation belong to the replaced file
        (the idle loop) and are discarded. mpv's `path` property echoes the
        argument given to loadfile, which is exactly `video_path`.

        Args:
            player: The mpv handle.
            video_path: Path string previously passed to player.play().
            skip_event: Controller skip signal (honored during load).
            stop_event: Controller stop signal (honored during load).

        Returns:
            None once the file is confirmed loaded; a PlaybackOutcome if
            playback ended first (stop/skip/load failure).
        """
        deadline = time.monotonic() + LOAD_TIMEOUT
        while time.monotonic() < deadline:
            if stop_event.is_set():
                logger.info("Stop requested while loading")
                self._stop_mpv(player)
                return PlaybackOutcome.STOPPED
            if skip_event.is_set():
                logger.info("Skip requested while loading")
                skip_event.clear()
                self._stop_mpv(player)
                return PlaybackOutcome.SKIPPED
            if player.path == video_path:
                with self._state_lock:
                    self._end_reason = None  # discard the replaced file's end
                self._load_confirmed.set()
                logger.debug("mpv confirmed file loaded")
                return None
            time.sleep(POLL_INTERVAL)

        logger.error(f"mpv did not load file within {LOAD_TIMEOUT}s: {video_path}")
        self._stop_mpv(player)
        return PlaybackOutcome.FAILED

    def _monitor_playback(
        self,
        player,
        skip_event: threading.Event,
        stop_event: threading.Event,
    ) -> PlaybackOutcome:
        """Poll until the loaded song ends one way or another.

        Args:
            player: The mpv handle.
            skip_event: Controller skip signal.
            stop_event: Controller stop signal.

        Returns:
            The PlaybackOutcome for this song.
        """
        playback_start = time.monotonic()
        while True:
            if time.monotonic() - playback_start > MAX_SONG_DURATION:
                logger.warning("Max song duration exceeded - advancing")
                self._stop_mpv(player)
                return PlaybackOutcome.TIMED_OUT

            if stop_event.is_set():
                logger.info("Stop requested during playback")
                self._stop_mpv(player)
                return PlaybackOutcome.STOPPED

            if skip_event.is_set():
                logger.info("Skip requested")
                skip_event.clear()
                self._stop_mpv(player)
                return PlaybackOutcome.SKIPPED

            with self._state_lock:
                reason = self._end_reason
            if reason is not None:
                if reason == "eof":
                    logger.info("Finished playing")
                    return PlaybackOutcome.FINISHED
                logger.warning(f"mpv playback ended with reason '{reason}'")
                return PlaybackOutcome.FAILED

            time.sleep(POLL_INTERVAL)

    def _stop_mpv(self, player) -> None:
        """Issue mpv's stop command, tolerating errors (handle may be dying).

        Args:
            player: The mpv handle.
        """
        try:
            player.command("stop")
        except Exception as e:
            logger.warning(f"Error issuing mpv stop: {e}")

    def _on_end_file(self, event) -> None:
        """Record how playback ended. Runs on mpv's event thread.

        RECORDS ONLY - never starts or schedules anything (see module
        docstring). play() decides what a recorded ending means.

        Args:
            event: python-mpv event object (only as_dict() is used).
        """
        try:
            data = event.as_dict()
            reason = _normalize_reason(data.get("reason"))
        except Exception:
            reason = "unknown"
        logger.debug(f"mpv end-file: {reason}")
        with self._state_lock:
            self._end_reason = reason

    # ------------------------------------------------------------------
    # Idle screensaver
    # ------------------------------------------------------------------

    def _resolve_idle_path(self) -> Optional[Path]:
        """Validate the configured screensaver video (warned once, at startup).

        Returns:
            The idle video Path, or None when the screensaver is disabled.
        """
        configured = settings.idle_video_path
        if configured is None:
            logger.warning(
                "IDLE_VIDEO_PATH not set - screensaver disabled "
                "(black screen when idle)"
            )
            return None
        path = Path(configured)
        if not path.exists():
            logger.warning(f"Idle video not found: {path} - screensaver disabled")
            return None
        logger.info(f"Idle screensaver video: {path}")
        return path

    def _arm_idle_timer(self) -> None:
        """(Re)start the IDLE_DELAY countdown to the screensaver.

        Called only from threads we control (startup, play's exit, cleanup),
        never from mpv's event thread. No-op when the screensaver is disabled
        or mpv is unavailable.
        """
        if self._idle_path is None or self._player is None:
            return
        with self._state_lock:
            self._cancel_idle_timer_locked()
            timer = threading.Timer(IDLE_DELAY, self._start_idle)
            timer.daemon = True
            self._idle_timer = timer
            timer.start()

    def _cancel_idle_timer_locked(self) -> None:
        """Cancel a pending idle timer. Caller must hold _state_lock."""
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _start_idle(self) -> None:
        """Timer thread: start the screensaver loop unless a song is starting.

        Holds _state_lock across the idle load so a play() call entering at
        the same moment orders strictly after it (play sets _song_in_progress
        under this lock, then loads its song, which replaces the idle loop).
        """
        with self._state_lock:
            if self._song_in_progress:
                return
            self._idle_timer = None
            player = self._player
            idle_path = self._idle_path
            if player is None or idle_path is None:
                return
            try:
                player.loop_file = "inf"
                player.play(str(idle_path))
                logger.info(f"Idle screensaver started: {idle_path}")
            except Exception as e:
                logger.warning(f"Failed to start idle screensaver: {e}")

    # ------------------------------------------------------------------
    # Discovery stubs (mpv has no discoverable devices)
    # ------------------------------------------------------------------

    async def discover_devices(
        self, timeout: int = 10, keep_connection: bool = False
    ) -> List[Dict]:
        """No discoverable devices for local output.

        Args:
            timeout: Ignored.
            keep_connection: Ignored.

        Returns:
            Always [].
        """
        return []

    def select_device(self, device_uuid: str) -> bool:
        """No devices to select for local output.

        Args:
            device_uuid: Ignored.

        Returns:
            Always False.
        """
        return False
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_mpv_player.py -v`
Expected: ALL PASS. If `test_stale_end_event_from_replaced_idle_is_discarded` is
flaky, the discard in `_wait_for_load` is in the wrong order relative to
`_load_confirmed.set()` — the discard MUST happen before the event is set.

- [ ] **Step 5: Full suite + lint, then commit**

Run: `uv run pytest && uv run ruff check . && uv run ruff format .`

```bash
git add app/services/players/mpv_player.py tests/test_mpv_player.py
git commit -m "feat: add MpvPlayer backend with idle screensaver"
```

---

### Task 4: Factory, wiring, packaging, and docs

Everything becomes reachable: the singleton is built from `PLAYER_BACKEND`, the
lifespan runs `startup()`, python-mpv becomes an installable extra, and docs/config
templates are updated.

**Files:**
- Create: `app/services/players/factory.py`
- Modify: `app/services/playout.py:13-24` (imports), `:363-364` (singleton)
- Modify: `app/main.py:83-134` (lifespan startup + log gating)
- Modify: `pyproject.toml` (optional extra), `uv.lock` (via `uv lock`)
- Modify: `.env.example`, `CLAUDE.md`
- Test: `tests/test_players.py` (factory tests)

**Interfaces:**
- Consumes: `MpvPlayer` (Task 3), `ChromecastPlayer`, `settings.player_backend`
  (Task 2), `PlayoutService.startup()` (Task 1).
- Produces: `create_player(backend: str) -> Player` in
  `app.services.players.factory`; the module singleton
  `playout_service = PlayoutService(create_player(settings.player_backend))`.

- [ ] **Step 1: Write the failing factory tests**

Append to `tests/test_players.py`:

```python
def test_factory_creates_chromecast():
    """'chromecast' resolves to the Chromecast backend."""
    from app.services.players.chromecast_player import ChromecastPlayer
    from app.services.players.factory import create_player

    assert isinstance(create_player("chromecast"), ChromecastPlayer)


def test_factory_creates_mpv():
    """'mpv' resolves to the mpv backend (constructing it needs no libmpv)."""
    from app.services.players.factory import create_player
    from app.services.players.mpv_player import MpvPlayer

    assert isinstance(create_player("mpv"), MpvPlayer)


def test_factory_rejects_unknown_backend():
    """Defense in depth behind the settings validator."""
    import pytest as _pytest

    from app.services.players.factory import create_player

    with _pytest.raises(ValueError):
        create_player("vlc")
```

(`import pytest as _pytest` inside the test keeps the module's top-of-file imports
unchanged; if you prefer, add `import pytest` to the file header instead — either is
acceptable, pick one and keep ruff clean.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_players.py -v`
Expected: the three factory tests FAIL with `ModuleNotFoundError:
app.services.players.factory`.

- [ ] **Step 3: Implement factory and wiring**

Create `app/services/players/factory.py`:

```python
"""
Playback backend factory.

Resolves the PLAYER_BACKEND setting to a Player instance with lazy imports, so
the Chromecast path never imports python-mpv and the mpv path never imports
pychromecast. This function is the seam a future runtime backend toggle would
plug into.
"""

# standard library
import logging

# project imports
from app.services.players import Player

logger = logging.getLogger(__name__)


def create_player(backend: str) -> Player:
    """Instantiate the configured playback backend.

    Args:
        backend: Normalized backend name ('chromecast' or 'mpv').

    Returns:
        The backend Player instance (not yet started up).

    Raises:
        ValueError: If the backend name is unknown. The settings validator
            rejects unknown values at startup; this is defense in depth.
    """
    if backend == "chromecast":
        from app.services.players.chromecast_player import ChromecastPlayer

        logger.info("Playback backend: chromecast")
        return ChromecastPlayer()
    if backend == "mpv":
        from app.services.players.mpv_player import MpvPlayer

        logger.info("Playback backend: mpv")
        return MpvPlayer()
    raise ValueError(f"Unknown PLAYER_BACKEND: {backend}")
```

In `app/services/playout.py`:

1. Replace the import block (lines 20-23):

```python
# project imports
from app.config import settings
from app.database import get_db
from app.services.players import PlaybackOutcome, Player
from app.services.players.factory import create_player
```

(the direct `ChromecastPlayer` import is removed.)

2. Replace the singleton (last two lines of the file):

```python
# Global instance. The backend comes from PLAYER_BACKEND; a future runtime
# toggle would rebuild this via the same factory.
playout_service = PlayoutService(create_player(settings.player_backend))
```

In `app/main.py`:

1. Add the startup call directly after `playout_service.set_event_loop(...)`
(line 86):

```python
    # Acquire the backend's app-lifetime resources (mpv: persistent handle +
    # idle screensaver; Chromecast: no-op).
    playout_service.startup()
```

2. Wrap the Chromecast configuration block (lines 102-134, from
`# Check and log server configuration for Chromecast` through the closing
`logger.info("=" * 60)`) in a backend gate:

```python
    # Check and log server configuration for Chromecast
    if settings.player_backend == "chromecast":
        logger.info("=" * 60)
        logger.info("SERVER CONFIGURATION FOR CHROMECAST")
        # ... existing block, indented one level, unchanged ...
        logger.info("=" * 60)
    else:
        logger.info("Playback backend: mpv (local video output)")
```

(Indent the existing statements; do not alter their text. The mpv branch stays to
one line — MpvPlayer.startup() already logs its options and idle-video status.)

In `pyproject.toml`, add after the `dependencies` list (before the
`# No [build-system]` comment):

```toml
[project.optional-dependencies]
# Local video output backend (PLAYER_BACKEND=mpv). Needs the libmpv system
# library at runtime (Debian/Raspberry Pi OS: apt install libmpv2).
# Install on the Pi with: uv sync --extra mpv
mpv = ["python-mpv"]
```

Then regenerate the lockfile:

Run: `uv lock`
Expected: `uv.lock` gains a `python-mpv` entry under the `mpv` extra; no other
dependency changes.

In `.env.example`, append after the `LOG_LEVEL` line:

```
# Playback backend (default: chromecast). Set to 'mpv' for local video output
# (e.g. Raspberry Pi HDMI + projector). mpv requires the optional extra
# (uv sync --extra mpv) and the libmpv system library (apt install libmpv2).
PLAYER_BACKEND=chromecast

# mpv only: video file looped as an idle "screensaver" whenever nothing is
# playing (starts ~15s after playback ends, and shortly after app startup).
# Leave unset to disable (black screen when idle).
#IDLE_VIDEO_PATH=./data/idle.mp4
```

In `CLAUDE.md`:

1. In "Technology Stack", change the Media line to:

```markdown
- **Media**: yt-dlp (requires ffmpeg) + pychromecast; optional mpv backend
  (python-mpv via the `mpv` extra) for local HDMI output on a Raspberry Pi
```

2. In "File Organization", replace the `players/` line with:

```markdown
│   │   ├── players/         # Player contract + backends (factory-selected)
│   │   │   ├── chromecast_player.py  # Chromecast backend
│   │   │   ├── mpv_player.py         # mpv/DRM backend + idle screensaver
│   │   │   └── factory.py            # PLAYER_BACKEND -> Player
```

3. In "Required environment variables", add after `LOG_LEVEL`:

```markdown
- `PLAYER_BACKEND` - Playback backend: `chromecast` (default) or `mpv`
- `IDLE_VIDEO_PATH` - mpv only: looped idle screensaver video (unset = disabled)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_players.py tests/test_playout.py tests/test_main.py -v`
Expected: ALL PASS, including the untouched
`test_singleton_wires_chromecast_player` (default backend is chromecast, so the
factory-built singleton is still a ChromecastPlayer).

- [ ] **Step 5: Boot smoke test**

Run: `timeout 10 uv run uvicorn app.main:app --port 8765 2>&1 | head -40; true`
Expected: startup completes; log shows `Playback backend: chromecast`, the
SERVER CONFIGURATION FOR CHROMECAST block, and `Application startup complete`.
No tracebacks.

- [ ] **Step 6: Full suite + lint, then commit**

Run: `uv run pytest && uv run ruff check . && uv run ruff format .`

```bash
git add app/services/players/factory.py app/services/playout.py app/main.py \
  pyproject.toml uv.lock .env.example CLAUDE.md tests/test_players.py
git commit -m "feat: select playback backend via PLAYER_BACKEND factory"
```

---

### Task 5: Manual verification gate on the Raspberry Pi

No code. This is the acceptance checklist from the spec; it needs the Pi, the
projector, and a human. Report results back before the branch is merged.

**Setup:** on the Pi: `uv sync --extra mpv`, `apt install libmpv2` (if not present),
`.env` with `PLAYER_BACKEND=mpv` and `IDLE_VIDEO_PATH` pointing at a real video,
at least two downloaded videos in `data/videos/`.

- [ ] Boot the app: screensaver appears within ~15 seconds, looping.
- [ ] Queue two songs, start playback: screensaver is replaced by song 1.
- [ ] Between songs: no screensaver flash (gap is ~1 second of black).
- [ ] Skip during song 2: playback advances/ends within ~1 second.
- [ ] Stop playback: screensaver returns after ~15 seconds.
- [ ] Start playback with an empty queue (add a song after ~30 seconds):
      screensaver appears while waiting, is replaced when the song downloads
      and plays.
- [ ] Stop the app (systemd stop or Ctrl-C): clean exit, display released,
      no hung process.
- [ ] Check logs for: `mpv initialized`, `Idle screensaver started`, one
      `Playback outcome` line per song, no tracebacks.

---

## Verification (whole plan)

- `uv run pytest` — full suite green (expected: prior 207 passed + ~35 new).
- `uv run ruff check . && uv run ruff format --check .` — clean.
- `uv run pytest tests/test_mpv_player.py -v` — the new backend's suite green.
- Task 5 checklist completed on the Pi.
- Docker/Chromecast regression: `PLAYER_BACKEND` unset behaves identically to
  before (guard message string is the only user-visible change).
