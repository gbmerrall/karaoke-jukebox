# Playback Backend Abstraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `app/services/chromecast.py` into a device-independent `PlayoutService` (queue policy) and a `Player` interface with `ChromecastPlayer` as its first implementation, so a future `MpvPlayer` backend can be added without touching queue logic, routes, or templates.

**Architecture:** A `Player` protocol plus `PlaybackOutcome` enum form the contract between layers. `PlayoutService` owns the playout thread, queue fate decisions, retry caps, and sync-to-async DB bridges; `ChromecastPlayer` owns device discovery, connection, and playing one video to an outcome. Blocking-call-plus-threading-events model, identical to today's threading design.

**Tech Stack:** Python 3.13, FastAPI, pychromecast/zeroconf, pytest (existing suite), stdlib logging (project convention), uv for all commands.

**Spec:** `docs/superpowers/specs/2026-07-05-playback-backend-abstraction-design.md`

## Global Constraints

- Behavior-preserving refactor with ONE deliberate exception: media session not starting within 30s returns `FAILED` and counts toward the retry cap (was: infinite retry).
- All route URL shapes, JSON response bodies, and user-facing message strings stay byte-identical (including `"No Chromecast device selected"` — generalizing strings is the mpv spec's job).
- No `PLAYER_BACKEND` config flag, no factory, no mpv code (YAGNI — follow-up spec).
- This project uses stdlib `logging` (not loguru) — follow the existing pattern: `logger = logging.getLogger(__name__)`.
- Imports grouped stdlib / 3rd party / project, alphabetical within groups. Lines wrap at 100 chars. Google-style docstrings on methods.
- `MIN_PLAY_TIME_BEFORE_IDLE_CHECK` is dead code — delete, do not migrate.
- Spec refinement (agreed rationale): `MAX_SONG_DURATION` is defined in `app/services/players/__init__.py` (the shared contract module) rather than `playout.py`, because the player enforces it and importing it from `playout.py` would create a circular import. It remains shared policy.
- Run `make test` and `uv run ruff check .` before every commit. Commit messages: conventional style, no emojis, no co-author lines.
- Tasks 1-3 only ADD files; the old `app/services/chromecast.py` stays untouched and green until Task 4 flips callers and deletes it.

---

### Task 1: Playback contract module (`PlaybackOutcome` + `Player` protocol)

**Files:**
- Create: `app/services/players/__init__.py`
- Test: `tests/test_players.py`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces (later tasks depend on these exact names):
  - `PlaybackOutcome` enum: `FINISHED`, `SKIPPED`, `STOPPED`, `FAILED`, `TIMED_OUT`
  - `MAX_SONG_DURATION: int` (= 1200 seconds)
  - `Player` protocol: `supports_discovery: bool`, `selected_device_uuid: Optional[str]`,
    `connect() -> bool`,
    `play(video_id: str, skip_event: threading.Event, stop_event: threading.Event) -> PlaybackOutcome`,
    `cleanup() -> None`,
    `async discover_devices(timeout: int = 10, keep_connection: bool = False) -> List[Dict]`,
    `select_device(device_uuid: str) -> bool`

Note: ALL methods are required by the protocol. Backends without discovery hardware set
`supports_discovery = False` and implement `discover_devices` / `select_device` as trivial
stubs (return `[]` / `False`). Flat and explicit beats optional-method gymnastics.

- [ ] **Step 1: Write the failing test**

Create `tests/test_players.py`:

```python
"""Tests for the playback contract module (enum + Player protocol)."""

import threading
from typing import Dict, List, Optional

from app.services.players import MAX_SONG_DURATION, PlaybackOutcome, Player


def test_playback_outcome_members():
    """The enum names every way a song can end - the layer contract."""
    assert {o.name for o in PlaybackOutcome} == {
        "FINISHED",
        "SKIPPED",
        "STOPPED",
        "FAILED",
        "TIMED_OUT",
    }


def test_max_song_duration_is_twenty_minutes():
    """Policy constant preserved from the original service."""
    assert MAX_SONG_DURATION == 20 * 60


class _StubPlayer:
    """Minimal non-discovery backend proving the protocol is satisfiable."""

    supports_discovery = False

    def __init__(self):
        self.selected_device_uuid: Optional[str] = None

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


def test_stub_player_satisfies_protocol():
    """A structural (duck-typed) backend passes the runtime protocol check."""
    assert isinstance(_StubPlayer(), Player)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_players.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'app.services.players'`

- [ ] **Step 3: Write the implementation**

Create `app/services/players/__init__.py`:

```python
"""
Playback backend contract.

A Player turns "play this downloaded video" into device-specific action and reports
how playback ended as a PlaybackOutcome. Backends hold NO queue knowledge: they never
see a queue id, a song title, or the database. Queue policy (retry caps, what happens
after each outcome) lives in app/services/playout.py.
"""

import threading
from enum import Enum
from typing import Dict, List, Optional, Protocol, runtime_checkable

# Policy: the longest a single song may play before the backend gives up and returns
# TIMED_OUT. Lives here (not in playout.py) because backends enforce it in their wait
# loops and importing it from playout.py would be a circular import.
MAX_SONG_DURATION = 20 * 60  # seconds


class PlaybackOutcome(Enum):
    """Every way a single song's playback can end."""

    FINISHED = "finished"  # played to the end
    SKIPPED = "skipped"  # skip event observed during playback
    STOPPED = "stopped"  # stop event observed during playback
    FAILED = "failed"  # device error, bad media, or session never started
    TIMED_OUT = "timed_out"  # exceeded MAX_SONG_DURATION


@runtime_checkable
class Player(Protocol):
    """Contract between PlayoutService (queue policy) and a playback device.

    Backends without discoverable devices set supports_discovery = False and
    implement discover_devices/select_device as stubs returning [] / False.
    """

    supports_discovery: bool
    selected_device_uuid: Optional[str]

    def connect(self) -> bool:
        """Prepare the device for playback. Called once per playout thread.

        Returns:
            True if the device is ready; False aborts the playout thread.
        """
        ...

    def play(
        self,
        video_id: str,
        skip_event: threading.Event,
        stop_event: threading.Event,
    ) -> PlaybackOutcome:
        """Play one video, blocking until there is an outcome.

        The backend resolves its own video reference from video_id (URL for
        Chromecast, filesystem path for a local player). It must poll the two
        events and return SKIPPED/STOPPED promptly when they are set, clearing
        skip_event (but never stop_event) before returning.

        Args:
            video_id: YouTube video id of a previously downloaded file.
            skip_event: Set by the controller when an admin skips the song.
            stop_event: Set by the controller when playback is stopped.

        Returns:
            The PlaybackOutcome describing how playback ended. Implementations
            must catch their own exceptions and return FAILED.
        """
        ...

    def cleanup(self) -> None:
        """Release device resources. Called in the playout thread's finally."""
        ...

    async def discover_devices(
        self, timeout: int = 10, keep_connection: bool = False
    ) -> List[Dict]:
        """Scan for playback devices.

        Args:
            timeout: Scan duration in seconds.
            keep_connection: True when playback is active, so an existing
                device connection must not be torn down to scan.

        Returns:
            A list of {"name": str, "uuid": str} dicts (empty for backends
            with supports_discovery = False).
        """
        ...

    def select_device(self, device_uuid: str) -> bool:
        """Choose the output device for subsequent playback.

        Args:
            device_uuid: Backend-specific device identifier.

        Returns:
            True if accepted.
        """
        ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_players.py -v`
Expected: 3 passed

- [ ] **Step 5: Lint, full suite, commit**

```bash
uv run ruff check . && uv run ruff format --check .
make test
git add app/services/players/__init__.py tests/test_players.py
git commit -m "feat: add playback backend contract (Player protocol, PlaybackOutcome)"
```

Expected: ruff clean; 183 passed, 1 skipped (180 existing + 3 new).

---

### Task 2: Extract `ChromecastPlayer`

**Files:**
- Create: `app/services/players/chromecast_player.py`
- Test: `tests/test_chromecast_player.py`
- Reference (do NOT modify yet): `app/services/chromecast.py` — the code being extracted

**Interfaces:**
- Consumes: `PlaybackOutcome`, `MAX_SONG_DURATION`, `Player` from `app.services.players` (Task 1); `settings.get_video_url(video_id)` from `app.config`.
- Produces: `ChromecastPlayer` class satisfying the `Player` protocol; module-level `DiscoveryListener`, `POLL_INTERVAL`, `STATUS_REFRESH_DELAY`. Task 3's singleton constructs `ChromecastPlayer()`.

Behavior notes for the implementer:
- This is a verbatim extraction of the device mechanics from `app/services/chromecast.py` (`_connect_to_device`, the play/monitor block from `_playout_loop`, `discover_devices`, `select_device`, `DiscoveryListener`, disconnect cleanup) with booleans replaced by `PlaybackOutcome` returns.
- THE one deliberate behavior change lives here: session-not-started returns `FAILED` (old code `continue`d forever).
- The old `is_playing` guard in `discover_devices` becomes the `keep_connection` parameter — the controller passes its own `is_playing` down. Explicit state passing, no back-reference to the controller.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_chromecast_player.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_chromecast_player.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'app.services.players.chromecast_player'`

- [ ] **Step 3: Write the implementation**

Create `app/services/players/chromecast_player.py`. This is the device-mechanics half of the old `app/services/chromecast.py` — compare against it while writing; logic is moved verbatim except where marked:

```python
"""
Chromecast playback backend.

Device mechanics only: discovery, connection, and playing one video through to a
PlaybackOutcome. Queue policy (retry caps, item fate) lives in app/services/playout.py.

Hard-won playback details preserved from the original implementation:
- Use BUFFERED stream type (NOT "LIVE") for video files.
- MUST wait for the media session before monitoring.
- The status object is STALE immediately after session activation (it still holds the
  previous video's state); Chromecast sends the first fresh update within ~100ms, so we
  wait 500ms before trusting it.
- idle_reason distinguishes completion types: FINISHED = success, ERROR = failure,
  INTERRUPTED/None = new media loading (keep waiting).
"""

# standard library
import asyncio
import logging
import threading
import time
from typing import Dict, List, Optional
from uuid import UUID

# 3rd party
import pychromecast
from pychromecast import CastInfo
from pychromecast.discovery import AbstractCastListener, CastBrowser
from zeroconf import Zeroconf
from zeroconf.asyncio import AsyncZeroconf

# project imports
from app.config import settings
from app.services.players import MAX_SONG_DURATION, PlaybackOutcome

logger = logging.getLogger(__name__)

POLL_INTERVAL = 2  # seconds - how often to check playback status
STATUS_REFRESH_DELAY = 0.5  # seconds - wait for a fresh status after session start


class DiscoveryListener(AbstractCastListener):
    """Listener for Chromecast discovery events."""

    def __init__(self):
        """Initialize the listener."""
        self.devices: Dict[UUID, CastInfo] = {}

    def add_cast(self, uuid: UUID, service: str) -> None:
        """Called when a new cast device is discovered (browser holds the info)."""
        pass

    def remove_cast(self, uuid: UUID, service: str, cast_info: CastInfo) -> None:
        """Called when a cast device is removed."""
        if uuid in self.devices:
            del self.devices[uuid]

    def update_cast(self, uuid: UUID, service: str) -> None:
        """Called when a cast device is updated."""
        pass


class ChromecastPlayer:
    """Player backend that casts local video files to a Chromecast device."""

    supports_discovery = True

    def __init__(self):
        """Initialize the player with no device selected or connected."""
        self.discovered_devices: List[Dict] = []
        self.selected_device_uuid: Optional[str] = None
        self._cast: Optional[pychromecast.Chromecast] = None

    async def discover_devices(
        self, timeout: int = 10, keep_connection: bool = False
    ) -> List[Dict]:
        """Scan the network for Chromecast devices using CastBrowser + AsyncZeroconf.

        Args:
            timeout: Scan timeout in seconds.
            keep_connection: True while playback is active; the live connection is
                then kept (disconnecting it would kill the current song).

        Returns:
            List of {"name": str, "uuid": str} dicts.
        """
        if keep_connection:
            if self._cast:
                logger.warning("Scan requested during playback - keeping active connection")
        elif self._cast:
            # Disconnect an existing idle connection before scanning; a stale
            # connection conflicts with AsyncZeroconf.
            logger.info(f"Disconnecting existing Chromecast: {self._cast.name}")
            try:
                if not self._cast.is_idle:
                    self._cast.quit_app()
                self._cast.disconnect()
                logger.info("Existing Chromecast disconnected")
            except Exception as e:
                logger.warning(f"Error disconnecting existing Chromecast: {e}")
            self._cast = None

        logger.info("Scanning for Chromecast devices...")
        try:
            aiozc = AsyncZeroconf()
            listener = DiscoveryListener()
            browser = CastBrowser(listener, aiozc.zeroconf)
            browser.start_discovery()

            logger.info(f"Waiting {timeout} seconds for device discovery...")
            await asyncio.sleep(timeout)

            self.discovered_devices = [
                {"name": service.friendly_name, "uuid": str(service.uuid)}
                for service in browser.services.values()
            ]

            browser.stop_discovery()
            await aiozc.async_close()

            logger.info(f"Found {len(self.discovered_devices)} Chromecast device(s)")
            return self.discovered_devices

        except Exception as e:
            logger.error(f"Error discovering Chromecast devices: {e}", exc_info=True)
            return []

    def select_device(self, device_uuid: str) -> bool:
        """Select a Chromecast device for playback.

        Args:
            device_uuid: UUID of the device to select.

        Returns:
            True if the device was selected, False if the uuid is empty/unknown.
        """
        device_exists = any(d["uuid"] == device_uuid for d in self.discovered_devices)

        if device_exists or device_uuid:  # Allow setting even if not in cache
            self.selected_device_uuid = device_uuid
            logger.info(f"Selected Chromecast device: {device_uuid}")
            return True

        logger.warning(f"Device not found: {device_uuid}")
        return False

    def connect(self) -> bool:
        """Connect to the selected device. Called once per playout thread.

        Returns:
            True when connected; False if no device is selected or unreachable.
        """
        if not self.selected_device_uuid:
            logger.error("No Chromecast device selected")
            return False

        logger.info(f"Connecting to Chromecast: {self.selected_device_uuid}")
        cast = self._connect_to_device(self.selected_device_uuid)
        if not cast:
            return False

        self._cast = cast
        logger.info(f"Connected to Chromecast: {cast.name}")
        return True

    def play(
        self,
        video_id: str,
        skip_event: threading.Event,
        stop_event: threading.Event,
    ) -> PlaybackOutcome:
        """Cast one video and block until playback ends one way or another.

        Args:
            video_id: YouTube id of a downloaded video in data/videos/.
            skip_event: Set by the controller to skip; cleared here when honored.
            stop_event: Set by the controller to stop; never cleared here.

        Returns:
            The PlaybackOutcome. All exceptions are caught and become FAILED.
        """
        cast = self._cast
        if cast is None:
            logger.error("play() called with no connected Chromecast")
            return PlaybackOutcome.FAILED

        video_url = settings.get_video_url(video_id)
        logger.info(f"URL: {video_url}")

        try:
            # Use BUFFERED stream type for video files (not LIVE).
            cast.play_media(video_url, "video/mp4", stream_type="BUFFERED")

            logger.info("Waiting for media session...")
            session_started = cast.media_controller.session_active_event.wait(timeout=30)
            if not session_started:
                # BEHAVIOR CHANGE (spec'd bug fix): this used to retry forever;
                # now it is a counted failure so the retry cap can advance the queue.
                logger.warning("Media session did not start")
                return PlaybackOutcome.FAILED

            logger.info("Media session active, monitoring playback...")

            # The status right after session activation can be stale (previous video).
            time.sleep(STATUS_REFRESH_DELAY)
            logger.debug("Status refresh delay complete, starting monitoring")

            playback_start = time.monotonic()

            while True:
                if time.monotonic() - playback_start > MAX_SONG_DURATION:
                    logger.warning("Max song duration exceeded - advancing")
                    cast.media_controller.stop()
                    return PlaybackOutcome.TIMED_OUT

                if stop_event.is_set():
                    logger.info("Stop requested during playback")
                    cast.media_controller.stop()
                    return PlaybackOutcome.STOPPED

                if skip_event.is_set():
                    logger.info("Skip requested")
                    skip_event.clear()
                    cast.media_controller.stop()
                    return PlaybackOutcome.SKIPPED

                mc_status = cast.media_controller.status
                if mc_status:
                    state = mc_status.player_state
                    logger.debug(f"Player state: {state}")

                    if state == "IDLE":
                        idle_reason = mc_status.idle_reason

                        # INTERRUPTED / None mean new media is loading - keep waiting.
                        if idle_reason == "INTERRUPTED" or idle_reason is None:
                            logger.debug(
                                f"IDLE ({idle_reason}) - new media loading, continuing..."
                            )
                            time.sleep(POLL_INTERVAL)
                            continue

                        if idle_reason == "FINISHED":
                            logger.info("Finished playing")
                            return PlaybackOutcome.FINISHED

                        if idle_reason == "ERROR":
                            logger.error("Playback error reported by device")
                            return PlaybackOutcome.FAILED

                        logger.warning(f"Idle: {idle_reason} - treating as failure")
                        return PlaybackOutcome.FAILED

                    elif state == "UNKNOWN":
                        logger.warning("Unknown player state")
                        return PlaybackOutcome.FAILED

                time.sleep(POLL_INTERVAL)

        except Exception as e:
            logger.error(f"Error during playback: {e}", exc_info=True)
            return PlaybackOutcome.FAILED

    def cleanup(self) -> None:
        """Quit the cast app and disconnect. Safe to call when not connected."""
        cast = self._cast
        self._cast = None
        if not cast:
            return
        try:
            if not cast.is_idle:
                cast.quit_app()
            cast.disconnect()
            logger.info("Disconnected from Chromecast")
        except Exception as e:
            logger.warning(f"Error during cleanup: {e}")

    def _connect_to_device(self, device_uuid: str) -> Optional[pychromecast.Chromecast]:
        """Connect to a Chromecast device by UUID using CastBrowser.

        Args:
            device_uuid: UUID string of the target device.

        Returns:
            A connected Chromecast, or None if not found / on error.
        """
        try:
            zconf = Zeroconf()
            listener = DiscoveryListener()
            browser = CastBrowser(listener, zconf)
            browser.start_discovery()

            logger.info("Searching for Chromecast device...")
            time.sleep(5)

            cast = None
            for uuid, service in browser.services.items():
                if str(uuid) == device_uuid:
                    # get_listed_chromecasts spins up its own browser/zeroconf which
                    # must be stopped to avoid leaking an mDNS browser thread.
                    chromecasts, host_browser = pychromecast.get_listed_chromecasts(
                        friendly_names=[service.friendly_name]
                    )
                    try:
                        if chromecasts:
                            cast = chromecasts[0]
                            cast.wait()
                            logger.info(f"Connected to Chromecast: {cast.name}")
                    finally:
                        pychromecast.discovery.stop_discovery(host_browser)
                    break

            browser.stop_discovery()
            zconf.close()

            if not cast:
                logger.error(f"Chromecast not found: {device_uuid}")

            return cast

        except Exception as e:
            logger.error(f"Error connecting to Chromecast: {e}", exc_info=True)
            return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_chromecast_player.py -v`
Expected: 23 passed

- [ ] **Step 5: Lint, full suite, commit**

```bash
uv run ruff check . && uv run ruff format --check .
make test
git add app/services/players/chromecast_player.py tests/test_chromecast_player.py
git commit -m "feat: extract ChromecastPlayer backend from chromecast service"
```

Expected: ruff clean; full suite green (old chromecast tests still pass — nothing was modified).

---

### Task 3: `PlayoutService` (queue policy controller)

**Files:**
- Create: `app/services/playout.py`
- Test: `tests/test_playout.py`
- Reference (do NOT modify yet): `app/services/chromecast.py` — bridges and lifecycle being extracted

**Interfaces:**
- Consumes: `Player`, `PlaybackOutcome` from `app.services.players` (Task 1); `ChromecastPlayer` from Task 2 (singleton wiring only); `get_db` from `app.database`; `queue_manager.broadcast_queue_update` (late import inside bridges, as today).
- Produces (Task 4's callers depend on these exact names): `PlayoutService` class with `set_event_loop(loop)`, `async discover_devices(timeout=10)`, `select_device(uuid)`, `start_playback()`, `stop_playback()`, `skip_current()`, `shutdown(timeout=10.0)`, `is_playing: bool`, `selected_device_uuid` (property); module-level singleton `playout_service`; constant `MAX_PLAYBACK_RETRIES = 3`.

Behavior notes for the implementer:
- All user-facing message strings are copied byte-for-byte from `ChromecastService` (including `"No Chromecast device selected"`).
- The fate table replaces the old boolean pair: FINISHED/SKIPPED/TIMED_OUT → remove + reset failures; STOPPED → requeue, no failure count; FAILED → increment, requeue until `MAX_PLAYBACK_RETRIES`, then mark `completed`.
- The DB bridges move verbatim (`run_coroutine_threadsafe` against `main_loop`, 30s timeouts, late `queue_manager` import to avoid the circular dependency).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_playout.py`:

```python
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

    def connect(self):
        return self.connect_ok

    def play(self, video_id, skip_event, stop_event):
        self.played.append(video_id)
        return self.outcomes.pop(0)

    def cleanup(self):
        self.cleaned_up = True

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


def test_start_playback_requires_selected_device():
    """Starting without a selected device fails with the exact legacy message."""
    player = FakePlayer()
    player.selected_device_uuid = None
    service = PlayoutService(player)
    result = service.start_playback()
    assert result == {"success": False, "message": "No Chromecast device selected"}


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_playout.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'app.services.playout'`

- [ ] **Step 3: Write the implementation**

Create `app/services/playout.py`:

```python
"""
Device-independent playout controller.

Owns queue policy: which song plays next, what happens to a queue row after each
PlaybackOutcome, retry caps, and the background playout thread. Device mechanics live
behind the Player interface (app/services/players/).

Threading model (unchanged from the original chromecast service): FastAPI routes run
on the main asyncio loop; playback runs in one daemon thread; the thread reaches the
async database via run_coroutine_threadsafe against the main loop.
"""

# standard library
import asyncio
import logging
import threading
import time
from typing import Dict, List, Optional

# project imports
from app.database import get_db
from app.services.players import PlaybackOutcome, Player
from app.services.players.chromecast_player import ChromecastPlayer

logger = logging.getLogger(__name__)

# After this many consecutive failed playback attempts, a song is marked 'completed'
# and dropped from the active queue so it cannot block every other song behind it
# (head-of-line starvation).
MAX_PLAYBACK_RETRIES = 3

QUEUE_POLL_INTERVAL = 5  # seconds - wait between queue checks when the queue is empty
INTER_SONG_PAUSE = 1  # seconds - brief pause between songs


class PlayoutService:
    """Queue-policy controller that drives an injected Player backend."""

    def __init__(self, player: Player):
        """Initialize the controller around a playback backend.

        Args:
            player: The playback backend (e.g. ChromecastPlayer).
        """
        self.player = player

        # Playback state (thread-safe)
        self.is_playing = False
        self.playout_thread: Optional[threading.Thread] = None
        self.playout_lock = threading.Lock()
        self.skip_requested = threading.Event()
        self.stop_requested = threading.Event()

        # queue_id -> consecutive failure count. Only touched by the single
        # playout thread, so it needs no lock.
        self._failure_counts: Dict[int, int] = {}

        # Main event loop reference for cross-thread async calls
        self.main_loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def selected_device_uuid(self) -> Optional[str]:
        """Currently selected output device id (passthrough for /admin/status)."""
        return self.player.selected_device_uuid

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Store the main event loop for cross-thread async calls.

        Must be called during app startup from the main async context; the playout
        thread schedules coroutines on it via run_coroutine_threadsafe().

        Args:
            loop: The main asyncio event loop.
        """
        self.main_loop = loop
        logger.info("Main event loop reference set for PlayoutService")

    async def discover_devices(self, timeout: int = 10) -> List[Dict]:
        """Scan for output devices via the backend.

        Args:
            timeout: Scan timeout in seconds.

        Returns:
            Device dicts from the backend; [] for backends without discovery.
        """
        if not self.player.supports_discovery:
            return []
        return await self.player.discover_devices(
            timeout=timeout, keep_connection=self.is_playing
        )

    def select_device(self, device_uuid: str) -> bool:
        """Select the output device via the backend.

        Args:
            device_uuid: Backend-specific device identifier.

        Returns:
            True if the backend accepted the selection.
        """
        return self.player.select_device(device_uuid)

    def start_playback(self) -> Dict:
        """Start playback from the queue.

        Returns:
            Dict with 'success' and 'message' keys.
        """
        with self.playout_lock:
            if self.is_playing:
                return {"success": False, "message": "Playback is already active"}

            if not self.player.selected_device_uuid:
                return {"success": False, "message": "No Chromecast device selected"}

            self.is_playing = True
            self.stop_requested.clear()
            self.skip_requested.clear()

            self.playout_thread = threading.Thread(target=self._playout_loop, daemon=True)
            self.playout_thread.start()

            logger.info("Playback started")
            return {"success": True, "message": "Playback started"}

    def stop_playback(self) -> Dict:
        """Stop playback.

        Returns:
            Dict with 'success' and 'message' keys.
        """
        with self.playout_lock:
            if not self.is_playing:
                return {"success": False, "message": "Playback is not active"}

            self.is_playing = False
            self.stop_requested.set()
            logger.info("Stop signal sent to playout loop")

        return {"success": True, "message": "Playback stopped"}

    def skip_current(self) -> Dict:
        """Skip the currently playing song.

        Returns:
            Dict with 'success' and 'message' keys.
        """
        with self.playout_lock:
            if not self.is_playing:
                return {"success": False, "message": "Playback is not active"}

            self.skip_requested.set()
            logger.info("Skip signal sent to playout loop")

        return {"success": True, "message": "Skipping current song"}

    def shutdown(self, timeout: float = 10.0) -> None:
        """Stop playback and join the playout thread for a clean exit.

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

    def _playout_loop(self) -> None:
        """Background thread: play queue items until stopped.

        Waits (does not exit) while the queue is empty so playback resumes
        automatically when users add songs during a break.
        """
        logger.info("Playout thread started")
        connected = False

        try:
            if not self.player.connect():
                logger.error("Failed to connect to playback device, stopping playback")
                with self.playout_lock:
                    self.is_playing = False
                return
            connected = True

            while True:
                if self.stop_requested.is_set():
                    logger.info("Stop requested, exiting playback loop")
                    break

                queue = self._get_queue_sync()
                if not queue:
                    logger.info("Queue is empty, waiting for songs to be added...")
                    time.sleep(QUEUE_POLL_INTERVAL)
                    continue

                item = queue[0]
                queue_id = item["id"]
                title = item["title"]

                logger.info(f"Playing: {title}")
                self._update_status_sync(queue_id, "playing")

                try:
                    outcome = self.player.play(
                        item["video_id"], self.skip_requested, self.stop_requested
                    )
                except Exception as e:
                    # Backends must catch their own errors; this is the last line of
                    # defense so a misbehaving backend cannot kill the loop.
                    logger.error(f"Player raised during play: {e}", exc_info=True)
                    outcome = PlaybackOutcome.FAILED

                logger.info(f"Playback outcome for '{title}': {outcome.name}")
                self._apply_outcome(queue_id, title, outcome)

                time.sleep(INTER_SONG_PAUSE)

        except Exception as e:
            logger.error(f"Playout loop error: {e}", exc_info=True)

        finally:
            logger.info("Cleaning up playout thread...")
            with self.playout_lock:
                self.is_playing = False
            if connected:
                self.player.cleanup()
            logger.info("Playout thread finished")

    def _apply_outcome(self, queue_id: int, title: str, outcome: PlaybackOutcome) -> None:
        """Decide the fate of a queue row from its playback outcome.

        Args:
            queue_id: Primary key of the queue row.
            title: Song title (for logs only).
            outcome: How playback ended.
        """
        if outcome in (
            PlaybackOutcome.FINISHED,
            PlaybackOutcome.SKIPPED,
            PlaybackOutcome.TIMED_OUT,
        ):
            logger.info(f"Removing from queue: {title}")
            self._failure_counts.pop(queue_id, None)
            self._remove_from_queue_sync(queue_id)
        elif outcome is PlaybackOutcome.STOPPED:
            # Stopped by admin: keep it queued, do not count as a failure.
            logger.info(f"Keeping in queue: {title}")
            self._update_status_sync(queue_id, "queued")
        else:  # PlaybackOutcome.FAILED
            failures = self._failure_counts.get(queue_id, 0) + 1
            self._failure_counts[queue_id] = failures
            if failures >= MAX_PLAYBACK_RETRIES:
                logger.error(
                    f"Giving up on '{title}' after {failures} failed attempts; "
                    "marking completed so the queue can advance"
                )
                self._failure_counts.pop(queue_id, None)
                self._update_status_sync(queue_id, "completed")
            else:
                logger.info(f"Keeping in queue (attempt {failures}): {title}")
                self._update_status_sync(queue_id, "queued")

    def _get_queue_sync(self) -> List[Dict]:
        """Read active queue rows from the playout thread.

        Returns:
            Row dicts ordered by added_at (statuses 'queued' and 'playing').
        """
        self._require_loop()
        try:

            async def get_queue():
                async with get_db() as db:
                    cursor = await db.execute(
                        "SELECT id, video_id, title FROM queue "
                        "WHERE status != 'completed' ORDER BY added_at ASC"
                    )
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]

            future = asyncio.run_coroutine_threadsafe(get_queue(), self.main_loop)
            return future.result(timeout=30)

        except Exception as e:
            logger.error(f"Error getting queue: {e}", exc_info=True)
            return []

    def _remove_from_queue_sync(self, queue_id: int) -> None:
        """Delete a queue row from the playout thread and broadcast the change.

        Args:
            queue_id: Primary key of the row to delete.
        """
        self._require_loop()
        try:

            async def remove():
                async with get_db() as db:
                    await db.execute("DELETE FROM queue WHERE id = ?", (queue_id,))
                    await db.commit()

                # Import here to avoid circular dependency
                from app.services.queue_manager import queue_manager

                await queue_manager.broadcast_queue_update()

            future = asyncio.run_coroutine_threadsafe(remove(), self.main_loop)
            future.result(timeout=30)

        except Exception as e:
            logger.error(f"Error removing from queue: {e}", exc_info=True)

    def _update_status_sync(self, queue_id: int, status: str) -> None:
        """Update a queue row's status from the playout thread and broadcast.

        Args:
            queue_id: Primary key of the row to update.
            status: New status ('queued', 'playing', or 'completed').
        """
        self._require_loop()
        try:

            async def update():
                async with get_db() as db:
                    await db.execute(
                        "UPDATE queue SET status = ? WHERE id = ?", (status, queue_id)
                    )
                    await db.commit()

                from app.services.queue_manager import queue_manager

                await queue_manager.broadcast_queue_update()

            future = asyncio.run_coroutine_threadsafe(update(), self.main_loop)
            future.result(timeout=30)

        except Exception as e:
            logger.error(f"Error updating status: {e}", exc_info=True)

    def _require_loop(self) -> None:
        """Raise if set_event_loop() has not been called yet."""
        if not self.main_loop:
            raise RuntimeError(
                "PlayoutService not initialized - event loop not set. "
                "Call set_event_loop() during app startup."
            )


# Global instance (the mpv spec replaces this hardcoded wiring with a factory)
playout_service = PlayoutService(ChromecastPlayer())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_playout.py -v`
Expected: 24 passed

- [ ] **Step 5: Lint, full suite, commit**

```bash
uv run ruff check . && uv run ruff format --check .
make test
git add app/services/playout.py tests/test_playout.py
git commit -m "feat: add device-independent PlayoutService controller"
```

Expected: ruff clean; full suite green (old service and tests still untouched).

---

### Task 4: Rewire callers, delete the old service

**Files:**
- Modify: `app/routes/admin.py` (import at line 10; call sites at lines 60, 88, 123, 136, 149, 162-163)
- Modify: `app/main.py` (lifespan lines 83-86, shutdown lines 163-166, health lines 212-214)
- Modify: `tests/test_routes.py` (fixture line 541 and its docstring at 520-523)
- Delete: `app/services/chromecast.py`
- Delete: `tests/test_chromecast.py`

**Interfaces:**
- Consumes: `playout_service` from `app.services.playout` (Task 3) — methods `set_event_loop`, `shutdown`, `discover_devices`, `select_device`, `start_playback`, `stop_playback`, `skip_current`, attributes `is_playing`, `selected_device_uuid`.
- Produces: nothing new — after this task nothing imports `app.services.chromecast` and the file is gone.

- [ ] **Step 1: Update `app/routes/admin.py`**

Change the import (line 10):

```python
# OLD
from app.services.chromecast import chromecast_service
# NEW
from app.services.playout import playout_service
```

Then replace every `chromecast_service.` with `playout_service.` (6 call sites: `discover_devices` in scan, `select_device` in select, `start_playback`, `stop_playback`, `skip_current`, and `is_playing`/`selected_device_uuid` in status). Also update the module docstring's first line to: `"""Admin routes for playback control and queue management."""` (keep the second line about admin authentication).

- [ ] **Step 2: Update `app/main.py`**

In `lifespan` (currently lines 83-86):

```python
# OLD
    # Set event loop reference for chromecast service (for sync/async bridge)
    from app.services.chromecast import chromecast_service

    chromecast_service.set_event_loop(asyncio.get_running_loop())
# NEW
    # Set event loop reference for the playout service (for sync/async bridge)
    from app.services.playout import playout_service

    playout_service.set_event_loop(asyncio.get_running_loop())
```

In the shutdown block (currently lines 163-166):

```python
# OLD
    # Stop playback and join the playout thread for a clean exit
    from app.services.chromecast import chromecast_service

    chromecast_service.shutdown()
# NEW
    # Stop playback and join the playout thread for a clean exit
    from app.services.playout import playout_service

    playout_service.shutdown()
```

In `health_check` (currently lines 212-218):

```python
# OLD
    from app.services.chromecast import chromecast_service

    return {
        "status": "healthy",
        "queue_size": await queue_manager.get_queue_size(),
        "is_playing": chromecast_service.is_playing,
    }
# NEW
    from app.services.playout import playout_service

    return {
        "status": "healthy",
        "queue_size": await queue_manager.get_queue_size(),
        "is_playing": playout_service.is_playing,
    }
```

- [ ] **Step 3: Update `tests/test_routes.py`**

In the `_admin_mocks` fixture, change line 541 and the docstring:

```python
# OLD (line 520-523 docstring)
    """Replace admin singletons with mocks and return them.

    Returns:
        Tuple of (chromecast_service mock, queue_manager mock).
    """
# NEW
    """Replace admin singletons with mocks and return them.

    Returns:
        Tuple of (playout_service mock, queue_manager mock).
    """

# OLD (line 541)
    monkeypatch.setattr(admin_module, "chromecast_service", cc)
# NEW
    monkeypatch.setattr(admin_module, "playout_service", cc)
```

The mock's method surface (`discover_devices`, `select_device`, `start_playback`, `stop_playback`, `skip_current`, `is_playing`, `selected_device_uuid`) already matches `PlayoutService` — no other changes.

- [ ] **Step 4: Delete the old module and its tests**

```bash
git rm app/services/chromecast.py tests/test_chromecast.py
```

- [ ] **Step 5: Prove nothing references the old module**

Run: `grep -rn "services.chromecast\|chromecast_service" app/ tests/ CLAUDE.md || echo CLEAN`
Expected: matches only in `CLAUDE.md` prose (updated in Step 6) or `CLEAN`. Any hit in `app/` or `tests/` is a missed call site — fix it before proceeding.

- [ ] **Step 6: Update CLAUDE.md references**

`CLAUDE.md` describes the old layout. Update these spots (prose edits, keep surrounding text):
- File Organization tree: replace the `chromecast.py   # Device discovery + playback thread` line with the two new entries `playout.py  # Queue policy + playout thread (device-independent)` and `players/    # Player contract + ChromecastPlayer backend`.
- References to `app/services/chromecast.py:_playout_loop()` (sections "Sync-to-Async Bridge", "Chromecast Playback State Machine", "Queue Item Removal Logic", "Modifying Queue Behavior"): point playback-policy notes at `app/services/playout.py:_playout_loop()` and device-mechanics notes (BUFFERED stream type, stale status, idle_reason) at `app/services/players/chromecast_player.py:play()`.
- "Chromecast Connection Management" section: discovery now lives in `app/services/players/chromecast_player.py:discover_devices()`.

- [ ] **Step 7: Run the full suite, lint, and type check**

```bash
make test
uv run ruff check . && uv run ruff format --check .
uv run ty check
```

Expected: 206 passed, 1 skipped (180 baseline - 24 deleted chromecast tests + 50 added in Tasks 1-3), ruff clean. `ty` should introduce no NEW errors relative to `main` (run `git stash && uv run ty check 2>&1 | tail -1 && git stash pop` for the baseline if unsure).

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "refactor: replace ChromecastService with PlayoutService + Player backend"
```

---

### Task 5: Manual Chromecast verification (deploy gate)

**Files:** none — this is hardware verification with a real Chromecast on the local network. CI cannot cover it.

**Interfaces:**
- Consumes: the fully wired app from Task 4.
- Produces: a verified refactor, ready for `superpowers:finishing-a-development-branch`.

- [ ] **Step 1: Start the dev server and log in as admin**

```bash
make run
```

Open `http://localhost:8000`, log in as `admin`, go to the admin panel.

- [ ] **Step 2: Device discovery and selection**

Click scan; confirm the real Chromecast appears and can be selected. In the server log, confirm the scan messages now come from `app.services.players.chromecast_player`.

- [ ] **Step 3: Full playback pass**

Queue two songs (search + add downloads them). Verify each of these against the fate table:
1. Start playback — first song plays on the TV; queue shows it as playing (SSE update).
2. Let song 1 finish naturally — it leaves the queue, song 2 starts (`FINISHED` path).
3. Skip song 2 — it leaves the queue immediately (`SKIPPED` path).
4. Queue another song, start playback, then stop mid-song — the song REMAINS queued (`STOPPED` path).
5. Start playback again — the same song resumes from the top (loop re-entry).
6. With playback active, run a device scan — playback must NOT be interrupted (`keep_connection` path).

- [ ] **Step 4: Shutdown behavior**

Ctrl-C the server during playback. Expected: "Shutting down playout service" and "Playout thread finished" in the log, Chromecast returns to its idle screen, no traceback.

- [ ] **Step 5: Record the result**

Note pass/fail for each check in the PR description (or commit message if no PR). Any failure: STOP, use superpowers:systematic-debugging before touching code.
