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
        # Guards selected_device_uuid and _cast, which are read/written from both
        # the request thread (select_device, discover_devices) and the playout
        # thread (connect, play, cleanup). Hold it only around the brief reference
        # read/write itself - never across blocking calls (network I/O, sleeps,
        # the play/monitor loop) - with ONE sanctioned exception: discover_devices
        # holds it across its disconnect-idle-connection block, matching the
        # pre-refactor behavior (that block ran under playout_lock).
        self._lock = threading.Lock()

    def startup(self) -> None:
        """No app-lifetime resources: the cast connection is per playout session."""
        return None

    def shutdown(self) -> None:
        """No app-lifetime resources: cleanup() already releases the session."""
        return None

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
        with self._lock:
            if keep_connection:
                if self._cast:
                    logger.warning(
                        "Scan requested during playback - keeping active connection"
                    )
            elif self._cast:
                # Disconnect an existing idle connection before scanning; a stale
                # connection conflicts with AsyncZeroconf. Held across this block
                # (matching the pre-refactor behavior) since it's a bounded,
                # request-thread-only disconnect, not the playout loop.
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
            with self._lock:
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
        with self._lock:
            device_uuid = self.selected_device_uuid

        if not device_uuid:
            logger.error("No Chromecast device selected")
            return False

        logger.info(f"Connecting to Chromecast: {device_uuid}")
        cast = self._connect_to_device(device_uuid)
        if not cast:
            return False

        with self._lock:
            self._cast = cast
        logger.info(f"Connected to Chromecast: {cast.name}")
        return True

    def play(
        self,
        video_id: str,
        skip_event: threading.Event,
        stop_event: threading.Event,
        next_up_text: Optional[str] = None,
    ) -> PlaybackOutcome:
        """Cast one video and block until playback ends one way or another.

        Args:
            video_id: YouTube id of a downloaded video in data/videos/.
            skip_event: Set by the controller to skip; cleared here when honored.
            stop_event: Set by the controller to stop; never cleared here.
            next_up_text: Ignored. Chromecast has no on-screen overlay support;
                this parameter exists only to satisfy the shared Player
                protocol so callers can pass it unconditionally.

        Returns:
            The PlaybackOutcome. All exceptions are caught and become FAILED.
        """
        with self._lock:
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
            session_started = cast.media_controller.session_active_event.wait(
                timeout=30
            )
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
        with self._lock:
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
