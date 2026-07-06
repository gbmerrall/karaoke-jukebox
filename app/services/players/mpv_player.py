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
            event: python-mpv event object (only as_dict() is used). Real
                python-mpv nests the event-specific payload under an "event"
                key (e.g. {"event": {"reason": ...}}); a flat {"reason": ...}
                is accepted too as a defensive fallback across versions.
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
