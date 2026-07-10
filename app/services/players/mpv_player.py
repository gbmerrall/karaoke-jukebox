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
import os
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
    "audio_device": "alsa/sysdefault:CARD=iBassoDCSeries",
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
        self._current_options: Dict[str, str] = dict(MPV_OPTIONS)
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

    def select_output(
        self, drm_device: str, drm_connector: str, audio_device: str
    ) -> Tuple[bool, str]:
        """Switch the local video/audio output, recreating the mpv handle.

        Rejected while a song is playing (tearing down the handle mid-song
        would kill in-flight playback with no recovery). Allowed any time
        the handle is idle or looping the screensaver. Not persisted: the
        selection resets to MPV_OPTIONS on the next app restart.

        Ordering depends on whether the new drm_device is the SAME as the
        current one:
        - Same device (e.g. an audio-only change, or re-selecting the
          current output): the old handle is terminated BEFORE building the
          new one, since mpv holds DRM master on that card and two live
          handles cannot claim it at once. A failed build then leaves
          self._player as None (no video until the app is restarted or
          select_output is called again), matching a failed startup().
        - Different device: the new handle is built BEFORE the old one is
          terminated, so a failed build leaves the old (different-card)
          handle fully intact and working.

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
            same_device = drm_device == self._current_options["drm_device"]
            new_options = dict(self._current_options)
            new_options["drm_device"] = drm_device
            new_options["drm_connector"] = drm_connector
            new_options["audio_device"] = audio_device

            if same_device:
                # Same DRM card: mpv holds it as DRM master, so the old
                # handle must release it before the new one can claim it.
                if old_player is not None:
                    try:
                        old_player.terminate()
                    except Exception as e:
                        logger.warning(f"Error terminating replaced mpv handle: {e}")
                new_player = self._build_handle(new_options)
                if new_player is None:
                    self._player = None
                    ready = False
                else:
                    self._player = new_player
                    self._current_options = new_options
                    ready = True
            else:
                # Different card: safe to build first so a failed build
                # leaves the still-valid old handle in place.
                new_player = self._build_handle(new_options)
                if new_player is None:
                    ready = False
                else:
                    if old_player is not None:
                        try:
                            old_player.terminate()
                        except Exception as e:
                            logger.warning(
                                f"Error terminating replaced mpv handle: {e}"
                            )
                    self._player = new_player
                    self._current_options = new_options
                    ready = True

        # Lock released: _arm_idle_timer() acquires it itself.
        self._arm_idle_timer()

        if not ready:
            return (False, "Failed to initialize mpv with the selected output")
        logger.info(f"mpv output switched: {drm_device} {drm_connector} {audio_device}")
        return (True, "")

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
            logger.info(f"mpv loading: {video_path}")

            # mpv's `path` property reports the loaded file expanded to an
            # absolute path (cwd prepended), not the literal string given to
            # play() - confirmation must compare against that same form.
            load_outcome = self._wait_for_load(
                player, os.path.abspath(str(video_path)), skip_event, stop_event
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
            event: python-mpv event object (only as_dict() is used). In
                python-mpv >= 1.0 (the locked dependency) as_dict() is a FLAT
                map from libmpv's mpv_event_to_node: {"event": b"end-file",
                "reason": b"eof", ...}. The nested {"event": {"reason": ...}}
                form is the legacy 0.x API (e.g. Debian's apt-packaged
                python3-mpv) and is read as a fallback.
        """
        try:
            data = event.as_dict()
            raw = data.get("reason")
            if raw is None and isinstance(data.get("event"), dict):
                raw = data["event"].get("reason")
            reason = _normalize_reason(raw)
        except Exception:
            reason = "unknown"
        logger.debug(f"mpv end-file: {reason}")
        with self._state_lock:
            self._end_reason = reason

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
        if path.resolve().is_relative_to(settings.get_videos_dir().resolve()):
            # Still usable (cleanup may be disabled), but warn at boot rather
            # than letting the screensaver silently vanish hours later.
            logger.warning(
                f"Idle video {path} is inside {settings.get_videos_dir()} - the "
                "cleanup job deletes unreferenced .mp4 files there, so the "
                "screensaver may disappear after a few hours. Move it to the "
                "data/ root (e.g. data/idle.mp4)."
            )
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
