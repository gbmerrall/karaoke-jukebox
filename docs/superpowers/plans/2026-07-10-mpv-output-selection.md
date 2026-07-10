# mpv Local Output Selection (Admin) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an admin pick the Raspberry Pi's HDMI video output (DRM device + connector) and ALSA audio output at runtime from the `/admin` page, instead of the values hardcoded in `MPV_OPTIONS`.

**Architecture:** `MpvPlayer` gains enumeration methods (native sysfs walk for video, mpv's own `audio_device_list` for audio) and a `select_output()` method that recreates its persistent mpv handle with new options while blocked from doing so mid-song. `PlayoutService` gets thin pass-through wrapper methods (mirroring its existing `select_device`/`discover_devices`). Two new admin routes expose these, and a new admin.html card drives them. Nothing is persisted to disk — the selection resets to `MPV_OPTIONS` defaults on every app restart, exactly like Chromecast's `select_device()` today.

**Tech Stack:** Python 3.13, FastAPI, pytest, python-mpv (via injected fake module in tests), Jinja2 + HTMX/vanilla JS in templates.

## Global Constraints

- Selection is runtime-only; never persisted to disk, `.env`, or a database. (Spec Decision 1)
- Both video output (drm_device + drm_connector, always changed together) and audio output (audio_device) are in scope. `drm_mode` and `hwdec` stay hardcoded — out of scope. (Spec Decision 2)
- Changing output while a song is actively playing is rejected (`409`). Changing it while idle or during the screensaver loop is allowed. (Spec Decision 3)
- Enumeration is native Python only: video via `/sys/class/drm/card*-*/status`, audio via the existing mpv handle's `audio_device_list` property. No new subprocess calls, no new Dockerfile/system dependencies (no `modetest`, no `aplay`). (Spec Decision 4)
- New dedicated methods/routes (`list_video_outputs`, `list_audio_outputs`, `select_output`) — the `Player` protocol in `app/services/players/__init__.py` is NOT modified. Chromecast is completely unaffected. (Spec Decision 5)

Full design: `docs/superpowers/specs/2026-07-10-mpv-output-selection-design.md`

---

### Task 1: `list_video_outputs()` — native DRM enumeration

**Files:**
- Modify: `app/services/players/mpv_player.py`
- Test: `tests/test_mpv_player.py`

**Interfaces:**
- Produces: `MpvPlayer.__init__(self, mpv_module=None, drm_base_path: Optional[Path] = None)` — new optional `drm_base_path` param, stored as `self._drm_base_path`.
- Produces: `MpvPlayer.list_video_outputs(self) -> List[Dict[str, str]]`, each dict shaped `{"drm_device": str, "drm_connector": str, "label": str}`.

- [ ] **Step 1: Write the failing tests**

Add to the end of `tests/test_mpv_player.py` (after the last test, before end of file):

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mpv_player.py -k list_video_outputs -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'drm_base_path'`

- [ ] **Step 3: Add the `re` import and `drm_base_path` constructor param**

In `app/services/players/mpv_player.py`, modify the standard-library import block (currently lines 22-28):

```python
# standard library
import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional
```

to:

```python
# standard library
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional
```

Then modify `__init__` (currently lines 82-104):

```python
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
```

to:

```python
    def __init__(self, mpv_module=None, drm_base_path: Optional[Path] = None):
        """Initialize without touching libmpv (that happens in startup()).

        Args:
            mpv_module: Module-like object exposing an MPV class. None
                (production) defers `import mpv` to startup(); tests inject a
                fake module.
            drm_base_path: Base sysfs path for DRM output enumeration.
                Defaults to /sys/class/drm; tests point this at a fake tree.
        """
        self.selected_device_uuid: Optional[str] = None
        self._mpv_module = mpv_module
        self._drm_base_path = drm_base_path or Path("/sys/class/drm")
        self._player = None  # persistent mpv.MPV handle, created in startup()
        self._idle_path: Optional[Path] = None
```

- [ ] **Step 4: Add `list_video_outputs()`**

Add this method to `MpvPlayer`, directly above the `# ----- Idle screensaver -----` section header (currently around line 386, right after `_on_end_file`):

```python
    def list_video_outputs(self) -> List[Dict[str, str]]:
        """Enumerate connected DRM video outputs (one entry per HDMI port).

        Walks /sys/class/drm/card*-*/status looking for connectors reporting
        "connected". Each Raspberry Pi HDMI port is its own DRM card device
        (not one card exposing multiple connectors), so device and connector
        are always returned as a coupled pair describing one physical port.

        Returns:
            List of {"drm_device": str, "drm_connector": str, "label": str}
            dicts, one per connected output. Empty if none are connected or
            the sysfs tree is unavailable (e.g. running off the Pi).
        """
        outputs = []
        base = self._drm_base_path
        if not base.is_dir():
            return outputs
        for entry in sorted(base.glob("card*-*")):
            match = re.match(r"^(card\d+)-(.+)$", entry.name)
            if match is None:
                continue
            card, connector = match.group(1), match.group(2)
            try:
                status = (entry / "status").read_text().strip()
            except OSError:
                continue
            if status != "connected":
                continue
            outputs.append(
                {
                    "drm_device": f"/dev/dri/{card}",
                    "drm_connector": connector,
                    "label": f"{connector} ({card})",
                }
            )
        return outputs
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `uv run pytest tests/test_mpv_player.py -k list_video_outputs -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Run the full mpv test file to check for regressions**

Run: `uv run pytest tests/test_mpv_player.py -v`
Expected: PASS (all tests, including pre-existing ones — `make_backend` doesn't pass `drm_base_path`, so the default `/sys/class/drm` applies and is untouched by existing tests)

- [ ] **Step 7: Type-check**

Run: `uv run ty check app/services/players/mpv_player.py`
Expected: no new errors

- [ ] **Step 8: Commit**

```bash
git add app/services/players/mpv_player.py tests/test_mpv_player.py
git commit -m "$(cat <<'EOF'
feat: enumerate connected DRM video outputs in MpvPlayer

Walks /sys/class/drm directly (no modetest/subprocess dependency) so
the admin UI can later offer a live list of HDMI ports instead of the
hardcoded card/connector pair.
EOF
)"
```

---

### Task 2: `list_audio_outputs()` — mpv's own ALSA enumeration

**Files:**
- Modify: `app/services/players/mpv_player.py`
- Test: `tests/test_mpv_player.py`

**Interfaces:**
- Consumes: the existing persistent handle at `self._player` (from Task 1's unchanged lifecycle); the handle's `audio_device_list` property (mpv-native, list of `{"name": str, "description": str}` dicts).
- Produces: `MpvPlayer.list_audio_outputs(self) -> List[Dict[str, str]]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mpv_player.py`, right after the Task 1 tests:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mpv_player.py -k list_audio_outputs -v`
Expected: FAIL — `AttributeError: 'MpvPlayer' object has no attribute 'list_audio_outputs'`

- [ ] **Step 3: Implement `list_audio_outputs()`**

Add this method to `MpvPlayer`, directly below `list_video_outputs()`:

```python
    def list_audio_outputs(self) -> List[Dict[str, str]]:
        """Enumerate ALSA audio outputs mpv itself can see.

        Queries the persistent handle's audio-device-list property (mpv
        already talks to ALSA), so no aplay/system-tool dependency is needed.

        Returns:
            List of {"name": str, "description": str} dicts (mpv's own audio
            device shape). Empty if mpv is unavailable or the query fails.
        """
        player = self._player
        if player is None:
            return []
        try:
            devices = player.audio_device_list
        except Exception as e:
            logger.warning(f"Failed to query mpv audio device list: {e}")
            return []
        return [
            {"name": d.get("name", ""), "description": d.get("description", "")}
            for d in devices or []
        ]
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `uv run pytest tests/test_mpv_player.py -k list_audio_outputs -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the full mpv test file to check for regressions**

Run: `uv run pytest tests/test_mpv_player.py -v`
Expected: PASS (all tests)

- [ ] **Step 6: Commit**

```bash
git add app/services/players/mpv_player.py tests/test_mpv_player.py
git commit -m "$(cat <<'EOF'
feat: enumerate ALSA audio outputs via mpv's audio_device_list

Reuses mpv's own device enumeration instead of shelling out to aplay,
keeping output selection dependency-free.
EOF
)"
```

---

### Task 3: `select_output()` — live handle recreation, guarded and race-safe

**Files:**
- Modify: `app/services/players/mpv_player.py`
- Test: `tests/test_mpv_player.py`

**Interfaces:**
- Consumes: `self._build_handle` (new, extracted from `startup()`), `self._state_lock`, `self._song_in_progress`, `self._arm_idle_timer()`, `self._cancel_idle_timer_locked()` (all pre-existing).
- Produces: `MpvPlayer.select_output(self, drm_device: str, drm_connector: str, audio_device: str) -> Tuple[bool, str]` — later tasks (admin routes) call this exact signature and unpack `(success, message)`. Return type matches `PlayoutService.select_output`'s annotation in Task 4.
- Produces: `MpvPlayer._current_options: Dict[str, str]` — the live options dict, starting as a copy of `MPV_OPTIONS`.

This is the task that introduces the ability to swap `self._player` outside `startup()`/`shutdown()` for the first time. `play()` currently captures `player = self._player` *before* acquiring `_state_lock` — safe today because the handle never changes after `startup()`, but unsafe once `select_output()` can replace it mid-flight. This task fixes that by moving the capture inside the lock, alongside where `_song_in_progress` is already set.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mpv_player.py`, right after the Task 2 tests:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mpv_player.py -k select_output -v`
Expected: FAIL — `AttributeError: 'MpvPlayer' object has no attribute 'select_output'`

- [ ] **Step 3: Add `Tuple` to the typing import and `_current_options` to `__init__`**

Modify the typing import (as changed by Task 1's edits, currently):

```python
from typing import Dict, List, Optional
```

to:

```python
from typing import Dict, List, Optional, Tuple
```

Then modify `__init__` again (this is the version Task 1 already changed):

```python
        self.selected_device_uuid: Optional[str] = None
        self._mpv_module = mpv_module
        self._drm_base_path = drm_base_path or Path("/sys/class/drm")
        self._player = None  # persistent mpv.MPV handle, created in startup()
        self._idle_path: Optional[Path] = None
```

to:

```python
        self.selected_device_uuid: Optional[str] = None
        self._mpv_module = mpv_module
        self._drm_base_path = drm_base_path or Path("/sys/class/drm")
        self._player = None  # persistent mpv.MPV handle, created in startup()
        self._current_options: Dict[str, str] = dict(MPV_OPTIONS)
        self._idle_path: Optional[Path] = None
```

- [ ] **Step 4: Extract `_build_handle()` and rewrite `startup()`**

Replace the current `startup()` (lines 110-143 as originally read):

```python
    def startup(self) -> None:
        """Create the persistent mpv handle and arm the idle screensaver.

        Called once from the app lifespan. Never raises: on failure the
        player logs, stays unavailable, and connect() returns False so the
        playout loop aborts cleanly while the admin UI stays reachable.
        """
        player = None
        try:
            mpv_module = self._mpv_module
            if mpv_module is None:
                # Deferred import: requires libmpv (install the 'mpv' extra).
                # ty can't resolve it in dev envs (the extra is Pi-only).
                import mpv as mpv_module  # ty: ignore[unresolved-import]
            player = mpv_module.MPV(**MPV_OPTIONS)
            player.event_callback("end-file")(self._on_end_file)
        except Exception as e:
            logger.error(f"mpv initialization failed: {e}", exc_info=True)
            if player is not None:
                # A created handle holds the DRM device; release it or the
                # display stays claimed until the app restarts.
                try:
                    player.terminate()
                except Exception as term_error:
                    logger.warning(f"Error terminating failed mpv init: {term_error}")
            self._player = None
            return

        self._player = player
        logger.info(f"mpv initialized with options: {MPV_OPTIONS}")

        self._idle_path = self._resolve_idle_path()
        if self._idle_path is not None:
            self._arm_idle_timer()
```

with:

```python
    def startup(self) -> None:
        """Create the persistent mpv handle and arm the idle screensaver.

        Called once from the app lifespan. Never raises: on failure the
        player logs, stays unavailable, and connect() returns False so the
        playout loop aborts cleanly while the admin UI stays reachable.
        """
        player = self._build_handle(self._current_options)
        if player is None:
            self._player = None
            return

        self._player = player
        logger.info(f"mpv initialized with options: {self._current_options}")

        self._idle_path = self._resolve_idle_path()
        if self._idle_path is not None:
            self._arm_idle_timer()

    def _build_handle(self, options: Dict[str, str]):
        """Construct and wire a new mpv handle. Never raises.

        Args:
            options: Options passed to mpv.MPV(**options) - MPV_OPTIONS, or
                a copy with drm_device/drm_connector/audio_device overridden.

        Returns:
            The new handle, or None on failure (logged; a partially
            constructed handle is terminated so it releases the DRM device).
        """
        player = None
        try:
            mpv_module = self._mpv_module
            if mpv_module is None:
                # Deferred import: requires libmpv (install the 'mpv' extra).
                # ty can't resolve it in dev envs (the extra is Pi-only).
                import mpv as mpv_module  # ty: ignore[unresolved-import]
            player = mpv_module.MPV(**options)
            player.event_callback("end-file")(self._on_end_file)
            return player
        except Exception as e:
            logger.error(f"mpv initialization failed: {e}", exc_info=True)
            if player is not None:
                # A created handle holds the DRM device; release it or the
                # display stays claimed until the app restarts.
                try:
                    player.terminate()
                except Exception as term_error:
                    logger.warning(f"Error terminating failed mpv init: {term_error}")
            return None
```

- [ ] **Step 5: Run the full mpv test file to confirm the refactor has no regressions**

Run: `uv run pytest tests/test_mpv_player.py -v`
Expected: PASS (all previously-passing tests still pass; the new `select_output` tests still fail — that's expected until Step 8)

- [ ] **Step 6: Fix the `play()` race — capture `self._player` under the lock**

Replace this block in `play()` (originally lines 210-230):

```python
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
```

with:

```python
        if self._player is None:
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
            # Captured under the same lock select_output() holds while
            # swapping self._player, so a handle recreated concurrently with
            # this call is never operated on via a stale, terminated
            # reference (the pre-lock check above is just a fast path).
            player = self._player

        try:
            player.loop_file = "no"
            player.play(str(video_path))
```

- [ ] **Step 7: Run the full mpv test file again to confirm the reorder has no regressions**

Run: `uv run pytest tests/test_mpv_player.py -v`
Expected: PASS (all previously-passing tests still pass)

- [ ] **Step 8: Implement `select_output()`**

Add this method directly below `_build_handle()`:

```python
    def select_output(
        self, drm_device: str, drm_connector: str, audio_device: str
    ) -> Tuple[bool, str]:
        """Switch the local video/audio output, recreating the mpv handle.

        Rejected while a song is playing (tearing down the handle mid-song
        would kill in-flight playback with no recovery). Allowed any time
        the handle is idle or looping the screensaver. Not persisted: the
        selection resets to MPV_OPTIONS on the next app restart.

        The whole check-and-swap runs under _state_lock - the same lock
        play() now captures self._player under - so a concurrent play()
        either runs entirely against the old handle or entirely against the
        new one, never a stale reference to a handle this call terminated.

        Args:
            drm_device: e.g. "/dev/dri/card0".
            drm_connector: e.g. "HDMI-A-1".
            audio_device: mpv audio-device string, e.g.
                "alsa/sysdefault:CARD=iBassoDCSeries".

        Returns:
            (True, "") on success; (False, message) if rejected or the new
            handle failed to initialize.
        """
        with self._state_lock:
            if self._song_in_progress:
                return (
                    False,
                    "Cannot change output during playback. Stop or wait "
                    "for the current song to finish.",
                )
            self._cancel_idle_timer_locked()
            old_player = self._player
            new_options = dict(self._current_options)
            new_options["drm_device"] = drm_device
            new_options["drm_connector"] = drm_connector
            new_options["audio_device"] = audio_device
            new_player = self._build_handle(new_options)

            if new_player is None:
                ready = False
            else:
                if old_player is not None:
                    try:
                        old_player.terminate()
                    except Exception as e:
                        logger.warning(f"Error terminating replaced mpv handle: {e}")
                self._player = new_player
                self._current_options = new_options
                ready = True

        # Lock released: _arm_idle_timer() acquires it itself.
        self._arm_idle_timer()

        if not ready:
            return (False, "Failed to initialize mpv with the selected output")
        logger.info(
            f"mpv output switched: {drm_device} {drm_connector} {audio_device}"
        )
        return (True, "")
```

- [ ] **Step 9: Run the new tests to verify they pass**

Run: `uv run pytest tests/test_mpv_player.py -k select_output -v`
Expected: PASS (5 tests)

- [ ] **Step 10: Run the entire mpv test file**

Run: `uv run pytest tests/test_mpv_player.py -v`
Expected: PASS (all tests)

- [ ] **Step 11: Type-check**

Run: `uv run ty check app/services/players/mpv_player.py`
Expected: no new errors

- [ ] **Step 12: Commit**

```bash
git add app/services/players/mpv_player.py tests/test_mpv_player.py
git commit -m "$(cat <<'EOF'
feat: add MpvPlayer.select_output for live video/audio switching

Recreates the persistent mpv handle with a new drm_device/
drm_connector/audio_device, blocked while a song is playing. Moves
play()'s self._player capture inside _state_lock so a handle swapped
mid-flight is never touched via a stale, terminated reference.
EOF
)"
```

---

### Task 4: `PlayoutService` pass-through methods

**Files:**
- Modify: `app/services/playout.py`
- Test: `tests/test_playout.py`

**Interfaces:**
- Consumes: `MpvPlayer.list_video_outputs`, `list_audio_outputs`, `select_output` (from Tasks 1-3) — accessed via `getattr` so backends without them (Chromecast) degrade gracefully.
- Produces: `PlayoutService.list_video_outputs(self) -> List[Dict[str, str]]`, `PlayoutService.list_audio_outputs(self) -> List[Dict[str, str]]`, `PlayoutService.select_output(self, drm_device: str, drm_connector: str, audio_device: str) -> Tuple[bool, str]` — later tasks (admin routes) call these exact names on `playout_service`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_playout.py`, right after the `FakePlayer` class definition (after line 59, before `_item`):

```python
class FakeOutputPlayer(FakePlayer):
    """FakePlayer extended with mpv-style output-selection methods."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.select_output_result = (True, "")
        self.select_output_call = None

    def list_video_outputs(self):
        return [
            {
                "drm_device": "/dev/dri/card0",
                "drm_connector": "HDMI-A-1",
                "label": "HDMI-A-1 (card0)",
            }
        ]

    def list_audio_outputs(self):
        return [{"name": "auto", "description": "Autoselect device"}]

    def select_output(self, drm_device, drm_connector, audio_device):
        self.select_output_call = (drm_device, drm_connector, audio_device)
        return self.select_output_result
```

Then add these tests right after `test_discover_devices_without_capability_returns_empty` (originally ending at line 296):

```python
def test_list_video_outputs_delegates_when_supported():
    """Backends exposing list_video_outputs() are passed through."""
    player = FakeOutputPlayer()
    service = PlayoutService(player)
    assert service.list_video_outputs() == [
        {
            "drm_device": "/dev/dri/card0",
            "drm_connector": "HDMI-A-1",
            "label": "HDMI-A-1 (card0)",
        }
    ]


def test_list_video_outputs_empty_without_support():
    """Backends without the method (e.g. Chromecast) yield []."""
    player = FakePlayer()
    service = PlayoutService(player)
    assert service.list_video_outputs() == []


def test_list_audio_outputs_delegates_when_supported():
    """Backends exposing list_audio_outputs() are passed through."""
    player = FakeOutputPlayer()
    service = PlayoutService(player)
    assert service.list_audio_outputs() == [
        {"name": "auto", "description": "Autoselect device"}
    ]


def test_list_audio_outputs_empty_without_support():
    """Backends without the method yield []."""
    player = FakePlayer()
    service = PlayoutService(player)
    assert service.list_audio_outputs() == []


def test_select_output_delegates_when_supported():
    """select_output passes its args through and returns the backend's result."""
    player = FakeOutputPlayer()
    player.select_output_result = (True, "")
    service = PlayoutService(player)
    ok, message = service.select_output("/dev/dri/card0", "HDMI-A-1", "auto")
    assert ok is True
    assert message == ""
    assert player.select_output_call == ("/dev/dri/card0", "HDMI-A-1", "auto")


def test_select_output_rejects_without_support():
    """Backends without select_output() (e.g. Chromecast) reject cleanly."""
    player = FakePlayer()
    service = PlayoutService(player)
    ok, message = service.select_output("/dev/dri/card0", "HDMI-A-1", "auto")
    assert ok is False
    assert "does not support" in message.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_playout.py -k "video_outputs or audio_outputs or select_output" -v`
Expected: FAIL — `AttributeError: 'PlayoutService' object has no attribute 'list_video_outputs'`

- [ ] **Step 3: Add `Tuple` to the typing import**

In `app/services/playout.py`, modify the typing import (currently line 18):

```python
from typing import Dict, List, Optional
```

to:

```python
from typing import Dict, List, Optional, Tuple
```

- [ ] **Step 4: Implement the three wrapper methods**

Add these methods to `PlayoutService`, directly below `select_device` (after the current line 111):

```python
    def list_video_outputs(self) -> List[Dict[str, str]]:
        """List available local video outputs via the backend.

        Returns:
            Output dicts from the backend's list_video_outputs(); [] for
            backends without local-output selection (e.g. Chromecast).
        """
        list_fn = getattr(self.player, "list_video_outputs", None)
        if list_fn is None:
            return []
        return list_fn()

    def list_audio_outputs(self) -> List[Dict[str, str]]:
        """List available local audio outputs via the backend.

        Returns:
            Output dicts from the backend's list_audio_outputs(); [] for
            backends without local-output selection.
        """
        list_fn = getattr(self.player, "list_audio_outputs", None)
        if list_fn is None:
            return []
        return list_fn()

    def select_output(
        self, drm_device: str, drm_connector: str, audio_device: str
    ) -> Tuple[bool, str]:
        """Select the local video/audio output via the backend.

        Args:
            drm_device: Backend-specific video device identifier.
            drm_connector: Backend-specific connector identifier.
            audio_device: Backend-specific audio device identifier.

        Returns:
            (True, "") on success; (False, message) on rejection, failure,
            or when the backend does not support output selection.
        """
        select_fn = getattr(self.player, "select_output", None)
        if select_fn is None:
            return (False, "This backend does not support output selection")
        return select_fn(drm_device, drm_connector, audio_device)
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `uv run pytest tests/test_playout.py -k "video_outputs or audio_outputs or select_output" -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Run the entire playout test file**

Run: `uv run pytest tests/test_playout.py -v`
Expected: PASS (all tests)

- [ ] **Step 7: Type-check**

Run: `uv run ty check app/services/playout.py`
Expected: no new errors

- [ ] **Step 8: Commit**

```bash
git add app/services/playout.py tests/test_playout.py
git commit -m "$(cat <<'EOF'
feat: add PlayoutService pass-through for mpv output selection

Mirrors the existing discover_devices/select_device pattern: backends
without list_video_outputs/list_audio_outputs/select_output (e.g.
Chromecast) degrade to empty lists / a clear rejection message.
EOF
)"
```

---

### Task 5: Admin routes

**Files:**
- Modify: `app/routes/admin.py`
- Test: `tests/test_routes.py`

**Interfaces:**
- Consumes: `playout_service.list_video_outputs()`, `playout_service.list_audio_outputs()`, `playout_service.select_output(drm_device, drm_connector, audio_device) -> Tuple[bool, str]` (from Task 4).
- Produces: `GET /admin/mpv/outputs` -> `{"video": [...], "audio": [...]}` (404 when `settings.player_backend != "mpv"`). `POST /admin/mpv/output/select` (form fields `drm_device`, `drm_connector`, `audio_device`) -> `200 {"success": true, "message": "Output updated"}` / `409 {"success": false, "message": "..."}` / `404` on non-mpv backends.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_routes.py`, right after `test_admin_select_device_not_found` (originally ending at line 639):

```python
def test_admin_mpv_outputs_success(_admin_mocks, monkeypatch):
    """The mpv outputs endpoint returns the backend's video/audio lists."""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "player_backend", "mpv")
    cc, _ = _admin_mocks
    cc.list_video_outputs = Mock(
        return_value=[
            {
                "drm_device": "/dev/dri/card0",
                "drm_connector": "HDMI-A-1",
                "label": "HDMI-A-1 (card0)",
            }
        ]
    )
    cc.list_audio_outputs = Mock(
        return_value=[{"name": "auto", "description": "Autoselect device"}]
    )
    response = _admin_client().get("/admin/mpv/outputs")
    assert response.status_code == 200
    body = response.json()
    assert body["video"][0]["drm_connector"] == "HDMI-A-1"
    assert body["audio"][0]["name"] == "auto"


def test_admin_mpv_outputs_404_for_chromecast(_admin_mocks, monkeypatch):
    """The mpv outputs endpoint is unavailable on the Chromecast backend."""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "player_backend", "chromecast")
    response = _admin_client().get("/admin/mpv/outputs")
    assert response.status_code == 404


def test_admin_select_mpv_output_success(_admin_mocks, monkeypatch):
    """A successful output switch returns success:true."""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "player_backend", "mpv")
    cc, _ = _admin_mocks
    cc.select_output = Mock(return_value=(True, ""))
    response = _admin_client().post(
        "/admin/mpv/output/select",
        data={
            "drm_device": "/dev/dri/card0",
            "drm_connector": "HDMI-A-1",
            "audio_device": "auto",
        },
    )
    assert response.status_code == 200
    assert response.json()["success"] is True
    cc.select_output.assert_called_once_with("/dev/dri/card0", "HDMI-A-1", "auto")


def test_admin_select_mpv_output_rejected_during_playback(_admin_mocks, monkeypatch):
    """A rejected switch (e.g. song playing) returns 409."""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "player_backend", "mpv")
    cc, _ = _admin_mocks
    cc.select_output = Mock(
        return_value=(False, "Cannot change output during playback.")
    )
    response = _admin_client().post(
        "/admin/mpv/output/select",
        data={
            "drm_device": "/dev/dri/card0",
            "drm_connector": "HDMI-A-1",
            "audio_device": "auto",
        },
    )
    assert response.status_code == 409
    assert response.json()["success"] is False


def test_admin_select_mpv_output_404_for_chromecast(_admin_mocks, monkeypatch):
    """Output selection is unavailable on the Chromecast backend."""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "player_backend", "chromecast")
    response = _admin_client().post(
        "/admin/mpv/output/select",
        data={
            "drm_device": "/dev/dri/card0",
            "drm_connector": "HDMI-A-1",
            "audio_device": "auto",
        },
    )
    assert response.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_routes.py -k "mpv_output" -v`
Expected: FAIL — `404 Not Found` on all four (routes don't exist yet)

- [ ] **Step 3: Implement the two routes**

Add to `app/routes/admin.py`, directly after `select_device` (after the current line 101, before `start_playback`):

```python
@router.get("/mpv/outputs")
async def list_mpv_outputs(request: Request):
    """
    List available local video/audio outputs (mpv backend only).

    Returns:
        JSON {"video": [...], "audio": [...]}; 404 on other backends.
    """
    require_admin(request)

    if settings.player_backend != "mpv":
        return JSONResponse({"video": [], "audio": []}, status_code=404)

    return JSONResponse(
        {
            "video": playout_service.list_video_outputs(),
            "audio": playout_service.list_audio_outputs(),
        }
    )


@router.post("/mpv/output/select")
async def select_mpv_output(
    request: Request,
    drm_device: str = Form(...),
    drm_connector: str = Form(...),
    audio_device: str = Form(...),
):
    """
    Select the local video/audio output (mpv backend only).

    Args:
        drm_device: DRM device path, e.g. "/dev/dri/card0".
        drm_connector: DRM connector name, e.g. "HDMI-A-1".
        audio_device: mpv audio-device string.
    """
    username, _ = require_admin(request)

    if settings.player_backend != "mpv":
        return JSONResponse(
            {
                "success": False,
                "message": "Output selection requires the mpv backend",
            },
            status_code=404,
        )

    logger.info(
        f"mpv output selection by {username}: "
        f"{drm_device} {drm_connector} {audio_device}"
    )
    success, message = playout_service.select_output(
        drm_device, drm_connector, audio_device
    )

    if success:
        return JSONResponse({"success": True, "message": "Output updated"})
    return JSONResponse({"success": False, "message": message}, status_code=409)
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `uv run pytest tests/test_routes.py -k "mpv_output" -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the entire routes test file**

Run: `uv run pytest tests/test_routes.py -v`
Expected: PASS (all tests)

- [ ] **Step 6: Type-check**

Run: `uv run ty check app/routes/admin.py`
Expected: no new errors

- [ ] **Step 7: Commit**

```bash
git add app/routes/admin.py tests/test_routes.py
git commit -m "$(cat <<'EOF'
feat: add admin routes for mpv output enumeration and selection

GET /admin/mpv/outputs and POST /admin/mpv/output/select, both 404 on
the Chromecast backend, mirroring the existing devices/scan and
devices/select routes.
EOF
)"
```

---

### Task 6: Admin template — output selection card

**Files:**
- Modify: `app/templates/admin.html`
- Test: `tests/test_routes.py`

**Interfaces:**
- Consumes: `GET /admin/mpv/outputs`, `POST /admin/mpv/output/select` (from Task 5); the existing `player_backend` template variable, and the existing `isPlaying`/`PLAYER_BACKEND` JS state already tracked by `pollPlaybackStatus()`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_routes.py`, right after `test_admin_page_chromecast_keeps_scan_and_gates_playback` (originally ending at line 596):

```python
def test_admin_page_mpv_renders_output_selection_controls(_admin_mocks, monkeypatch):
    """The mpv output card renders video/audio selects and an apply button."""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "player_backend", "mpv")
    html = _admin_client().get("/admin/").text
    assert 'id="mpv-video-select"' in html
    assert 'id="mpv-audio-select"' in html
    assert 'id="mpv-apply-btn"' in html


def test_admin_page_chromecast_omits_output_selection_controls(
    _admin_mocks, monkeypatch
):
    """The mpv-only output controls do not render on the Chromecast backend."""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "player_backend", "chromecast")
    html = _admin_client().get("/admin/").text
    assert 'id="mpv-video-select"' not in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_routes.py -k output_selection_controls -v`
Expected: FAIL for `test_admin_page_mpv_renders_output_selection_controls` (elements not in markup yet); `test_admin_page_chromecast_omits...` passes trivially (nothing to find either way) — note this, it will start meaningfully passing once Step 3 lands.

- [ ] **Step 3: Replace the placeholder card in `admin.html`**

Replace (currently lines 48-56):

```html
        {% if player_backend == 'mpv' %}
        <!-- Local HDMI output: mpv has no discoverable devices to pick.
             Output selection (connector/audio) will be added here later. -->
        <div class="card bg-base-100 shadow-xl">
            <div class="card-body">
                <h3 class="card-title text-lg">Output Device</h3>
                <p class="text-sm text-gray-500">HDMI playout</p>
            </div>
        </div>
        {% else %}
```

with:

```html
        {% if player_backend == 'mpv' %}
        <!-- Local HDMI output: video (drm_device+drm_connector) and audio
             device are two independent choices, populated from
             /admin/mpv/outputs and applied via /admin/mpv/output/select. -->
        <div class="card bg-base-100 shadow-xl">
            <div class="card-body">
                <h3 class="card-title text-lg">Output Device</h3>
                <p class="text-sm text-gray-500">HDMI playout</p>
                <label class="form-control w-full mt-2">
                    <span class="label-text">Video output</span>
                    <select id="mpv-video-select" class="select select-bordered w-full">
                        <option value="">Loading...</option>
                    </select>
                </label>
                <label class="form-control w-full mt-2">
                    <span class="label-text">Audio output</span>
                    <select id="mpv-audio-select" class="select select-bordered w-full">
                        <option value="">Loading...</option>
                    </select>
                </label>
                <button
                    id="mpv-apply-btn"
                    class="btn btn-primary btn-sm mt-2"
                    onclick="applyMpvOutput()"
                >
                    Apply
                </button>
                <p id="mpv-output-status" class="text-sm mt-2"></p>
            </div>
        </div>
        {% else %}
```

- [ ] **Step 4: Add the JS wiring**

In the `extra_scripts` block, modify this line (originally line 191):

```javascript
    const PLAYER_BACKEND = "{{ player_backend }}";
```

to:

```javascript
    const PLAYER_BACKEND = "{{ player_backend }}";

    // mpv output selection (video/audio) - only relevant for the mpv
    // backend. Uses plain fetch (not hx-get) because the endpoints return
    // JSON, matching the existing device-select fetch() pattern below
    // rather than an HTMX HTML-swap.
    function loadMpvOutputs() {
        if (PLAYER_BACKEND !== 'mpv') {
            return;
        }
        fetch('/admin/mpv/outputs')
            .then(response => response.json())
            .then(data => {
                populateOutputSelect(
                    'mpv-video-select',
                    data.video,
                    item => `${item.drm_device}|${item.drm_connector}`,
                    item => item.label
                );
                populateOutputSelect(
                    'mpv-audio-select',
                    data.audio,
                    item => item.name,
                    item => item.description || item.name
                );
            })
            .catch(err => console.error('Error loading mpv outputs:', err));
    }

    function populateOutputSelect(selectId, items, valueFn, labelFn) {
        const select = document.getElementById(selectId);
        if (!select) {
            return;
        }
        if (!items || items.length === 0) {
            select.innerHTML = '<option value="">No outputs detected</option>';
            return;
        }
        select.innerHTML = items
            .map(item => `<option value="${valueFn(item)}">${labelFn(item)}</option>`)
            .join('');
    }

    function applyMpvOutput() {
        const videoSelect = document.getElementById('mpv-video-select');
        const audioSelect = document.getElementById('mpv-audio-select');
        const status = document.getElementById('mpv-output-status');
        const [drmDevice, drmConnector] = (videoSelect.value || '').split('|');
        const audioDevice = audioSelect.value;
        if (!drmDevice || !drmConnector || !audioDevice) {
            status.textContent = 'Select a video and audio output first';
            status.className = 'text-sm text-warning mt-2';
            return;
        }
        fetch('/admin/mpv/output/select', {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: 'drm_device=' + encodeURIComponent(drmDevice)
                + '&drm_connector=' + encodeURIComponent(drmConnector)
                + '&audio_device=' + encodeURIComponent(audioDevice),
        })
        .then(response => response.json())
        .then(data => {
            status.textContent = data.message;
            status.className = data.success
                ? 'text-sm text-success mt-2'
                : 'text-sm text-error mt-2';
        })
        .catch(err => console.error('Error applying mpv output:', err));
    }

    function updateMpvApplyButton() {
        const btn = document.getElementById('mpv-apply-btn');
        if (!btn) {
            return;
        }
        btn.disabled = isPlaying;
        btn.title = isPlaying ? 'Cannot change output during playback' : '';
    }
```

Then, inside `pollPlaybackStatus()`, modify this block (originally lines 221-225):

```javascript
                // If playback state changed, update UI
                if (wasPlaying !== isPlaying) {
                    console.log('Playback state changed:', isPlaying ? 'playing' : 'stopped');
                    updateScanButton();
                }
```

to:

```javascript
                // If playback state changed, update UI
                if (wasPlaying !== isPlaying) {
                    console.log('Playback state changed:', isPlaying ? 'playing' : 'stopped');
                    updateScanButton();
                    updateMpvApplyButton();
                }
```

Finally, modify the initial-poll lines (originally lines 232-236):

```javascript
    // Start polling every 2 seconds
    setInterval(pollPlaybackStatus, 2000);

    // Do initial poll
    pollPlaybackStatus();
```

to:

```javascript
    // Start polling every 2 seconds
    setInterval(pollPlaybackStatus, 2000);

    // Do initial poll
    pollPlaybackStatus();

    // Populate the mpv output dropdowns and disable Apply if already playing.
    loadMpvOutputs();
    updateMpvApplyButton();
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `uv run pytest tests/test_routes.py -k output_selection_controls -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Run the entire routes test file**

Run: `uv run pytest tests/test_routes.py -v`
Expected: PASS (all tests)

- [ ] **Step 7: Commit**

```bash
git add app/templates/admin.html tests/test_routes.py
git commit -m "$(cat <<'EOF'
feat: add video/audio output selection to the admin mpv card

Populates two dropdowns from /admin/mpv/outputs and applies a choice
via /admin/mpv/output/select. Apply is disabled while a song is
playing, matching the route's 409-during-playback behavior.
EOF
)"
```

---

### Task 7: Full verification pass

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest`
Expected: PASS (all tests, fast suite only - no network)

- [ ] **Step 2: Run the type checker across the whole project**

Run: `uv run ty check`
Expected: no errors

- [ ] **Step 3: Run lint**

Run: `make lint`
Expected: no errors (ruff check + ruff format --check both clean)

- [ ] **Step 4: Manual smoke check (optional but recommended before Pi deployment)**

Run: `make run`, log in as admin with `PLAYER_BACKEND=mpv` set, open `/admin`, and confirm:
- The "Output Device" card shows two dropdowns and an Apply button.
- On a dev machine without `/sys/class/drm` populated with connected outputs, both dropdowns show "No outputs detected" (expected off-Pi) rather than erroring.
- The Apply button is disabled while `is_playing` is true (check via `/admin/status`).

This step has no automated assertion — it is a manual check, since the real DRM/ALSA enumeration can only be meaningfully exercised on the actual Raspberry Pi hardware.
