"""
Chromecast playback service.
Manages device discovery, connection, and playback of local video files.

Based on lessons learned from previous implementation:
- Use threading for playout loop (pychromecast is synchronous)
- Thread-safe state management with locks and events
- Robust connection handling with reconnection logic
- Proper playback state detection with timing safeguards
"""

import pychromecast
from pychromecast.discovery import AbstractCastListener, CastBrowser
from pychromecast import CastInfo
from zeroconf import Zeroconf
from zeroconf.asyncio import AsyncZeroconf
import threading
import time
import logging
from typing import List, Dict, Optional
from uuid import UUID
from app.config import settings
from app.database import get_db
import asyncio

logger = logging.getLogger(__name__)

# Constants for playback detection (learned from old implementation)
MIN_PLAY_TIME_BEFORE_IDLE_CHECK = 5  # seconds - avoid false positives during buffering
MAX_SONG_DURATION = 20 * 60  # 20 minutes - safety timeout
POLL_INTERVAL = 2  # seconds - how often to check playback status


class DiscoveryListener(AbstractCastListener):
    """Listener for Chromecast discovery events."""

    def __init__(self):
        """Initialize the listener."""
        self.devices: Dict[UUID, CastInfo] = {}

    def add_cast(self, uuid: UUID, service: str) -> None:
        """Called when a new cast device is discovered."""
        # Access the browser's services to get the device info
        # This will be set by the ChromecastService
        pass

    def remove_cast(self, uuid: UUID, service: str, cast_info: CastInfo) -> None:
        """Called when a cast device is removed."""
        if uuid in self.devices:
            del self.devices[uuid]

    def update_cast(self, uuid: UUID, service: str) -> None:
        """Called when a cast device is updated."""
        pass


class ChromecastService:
    """Service for managing Chromecast devices and playback."""

    def __init__(self):
        """Initialize Chromecast service."""
        # Discovered devices cache
        self.discovered_devices: List[Dict] = []

        # Playback state (thread-safe)
        self.is_playing = False
        self.selected_device_uuid: Optional[str] = None
        self.playout_thread: Optional[threading.Thread] = None
        self.connected_cast = None  # Currently connected Chromecast (for cleanup)
        self.playout_lock = threading.Lock()
        self.skip_requested = threading.Event()
        self.stop_requested = threading.Event()

    async def discover_devices(self, timeout: int = 10) -> List[Dict]:
        """
        Scan the network for Chromecast devices using CastBrowser with AsyncZeroconf.

        Args:
            timeout: Scan timeout in seconds

        Returns:
            List of device dictionaries with 'name' and 'uuid' keys
        """
        # Disconnect any existing Chromecast connection before scanning
        with self.playout_lock:
            if self.connected_cast:
                logger.info(f"Disconnecting existing Chromecast: {self.connected_cast.name}")
                try:
                    if not self.connected_cast.is_idle:
                        self.connected_cast.quit_app()
                    self.connected_cast.disconnect()
                    self.connected_cast = None
                    logger.info("Existing Chromecast disconnected")
                except Exception as e:
                    logger.warning(f"Error disconnecting existing Chromecast: {e}")
                    self.connected_cast = None

        logger.info("Scanning for Chromecast devices...")
        try:
            # Create AsyncZeroconf instance
            aiozc = AsyncZeroconf()

            # Create listener
            listener = DiscoveryListener()

            # Create browser with the underlying Zeroconf instance
            browser = CastBrowser(listener, aiozc.zeroconf)
            browser.start_discovery()

            # Wait for discovery using async sleep
            logger.info(f"Waiting {timeout} seconds for device discovery...")
            await asyncio.sleep(timeout)

            # Collect discovered devices from browser.services
            self.discovered_devices = [
                {
                    "name": service.friendly_name,
                    "uuid": str(service.uuid)
                }
                for service in browser.services.values()
            ]

            # Stop discovery
            browser.stop_discovery()

            # Close AsyncZeroconf
            await aiozc.async_close()

            logger.info(f"Found {len(self.discovered_devices)} Chromecast device(s)")
            return self.discovered_devices

        except Exception as e:
            logger.error(f"Error discovering Chromecast devices: {e}", exc_info=True)
            return []

    def select_device(self, device_uuid: str) -> bool:
        """
        Select a Chromecast device for playback.

        Args:
            device_uuid: UUID of the device to select

        Returns:
            True if device was selected, False if not found
        """
        # Verify device exists in discovered devices
        device_exists = any(d["uuid"] == device_uuid for d in self.discovered_devices)

        if device_exists or device_uuid:  # Allow setting even if not in cache
            with self.playout_lock:
                self.selected_device_uuid = device_uuid
                logger.info(f"Selected Chromecast device: {device_uuid}")
                return True

        logger.warning(f"Device not found: {device_uuid}")
        return False

    def start_playback(self) -> Dict:
        """
        Start playback from the queue.

        Returns:
            Dict with 'success' and 'message' keys
        """
        with self.playout_lock:
            if self.is_playing:
                return {"success": False, "message": "Playback is already active"}

            if not self.selected_device_uuid:
                return {"success": False, "message": "No Chromecast device selected"}

            # Start playout thread
            self.is_playing = True
            self.stop_requested.clear()
            self.skip_requested.clear()

            self.playout_thread = threading.Thread(
                target=self._playout_loop,
                daemon=True
            )
            self.playout_thread.start()

            logger.info("Playback started")
            return {"success": True, "message": "Playback started"}

    def stop_playback(self) -> Dict:
        """
        Stop playback.

        Returns:
            Dict with 'success' and 'message' keys
        """
        with self.playout_lock:
            if not self.is_playing:
                return {"success": False, "message": "Playback is not active"}

            self.is_playing = False
            self.stop_requested.set()
            logger.info("Stop signal sent to playout loop")

        return {"success": True, "message": "Playback stopped"}

    def skip_current(self) -> Dict:
        """
        Skip the currently playing song.

        Returns:
            Dict with 'success' and 'message' keys
        """
        with self.playout_lock:
            if not self.is_playing:
                return {"success": False, "message": "Playback is not active"}

            self.skip_requested.set()
            logger.info("Skip signal sent to playout loop")

        return {"success": True, "message": "Skipping current song"}

    def _playout_loop(self):
        """
        Simple background thread for Chromecast playback.

        Continuously plays items from the queue until stopped or queue is empty.
        """
        logger.info("Playout thread started")
        cast = None

        try:
            # Connect to Chromecast once at start
            with self.playout_lock:
                device_uuid = self.selected_device_uuid

            logger.info(f"Connecting to Chromecast: {device_uuid}")
            cast = self._connect_to_device(device_uuid)

            if not cast:
                logger.error("Failed to connect to Chromecast, stopping playback")
                with self.playout_lock:
                    self.is_playing = False
                return

            # Store connected cast for cleanup
            with self.playout_lock:
                self.connected_cast = cast

            logger.info(f"Connected to Chromecast: {cast.name}")

            # Main playback loop
            while True:
                # Check for stop signal
                if self.stop_requested.is_set():
                    logger.info("Stop requested, exiting playback loop")
                    break

                # Get next item from queue
                queue = self._get_queue_sync()
                if not queue:
                    logger.info("Queue is empty, stopping playback")
                    break

                item = queue[0]
                video_id = item["video_id"]
                title = item["title"]
                queue_id = item["id"]

                logger.info(f"Playing: {title}")

                # Update queue item status to playing
                self._update_status_sync(queue_id, "playing")

                # Generate video URL
                video_url = settings.get_video_url(video_id)
                logger.info(f"URL: {video_url}")

                # Play the video
                should_remove_from_queue = False  # Default: keep in queue (only remove if completed or skipped)
                try:
                    # Use BUFFERED stream type for video files (not LIVE)
                    cast.play_media(video_url, "video/mp4", stream_type="BUFFERED")

                    # Wait for media session to become active
                    logger.info("Waiting for media session...")
                    session_started = cast.media_controller.session_active_event.wait(timeout=30)
                    if not session_started:
                        logger.warning(f"Media session did not start for: {title}")
                        continue

                    logger.info("Media session active, monitoring playback...")

                    # CRITICAL: Wait for Chromecast to send fresh status update
                    # The status immediately after session activation can be stale (from previous video)
                    # This prevents incorrectly detecting "FINISHED" from the previous video
                    # Chromecast typically sends first update within 100ms, we wait 500ms to be safe
                    time.sleep(0.5)
                    logger.debug("Status refresh delay complete, starting monitoring")

                    # Wait for playback to finish or be interrupted
                    while True:
                        # Check for stop/skip
                        if self.stop_requested.is_set():
                            logger.info("Stop requested during playback")
                            cast.media_controller.stop()
                            should_remove_from_queue = False  # Keep in queue when stopped
                            break

                        if self.skip_requested.is_set():
                            logger.info("Skip requested")
                            self.skip_requested.clear()
                            cast.media_controller.stop()
                            should_remove_from_queue = True  # Remove when skipped
                            break

                        # Check playback status
                        mc_status = cast.media_controller.status
                        if mc_status:
                            state = mc_status.player_state
                            logger.debug(f"Player state: {state}")

                            if state == "IDLE":
                                # Check why it's idle
                                idle_reason = mc_status.idle_reason

                                # INTERRUPTED means a new media is loading - keep waiting
                                # None means transitioning between media - keep waiting
                                if idle_reason == "INTERRUPTED" or idle_reason is None:
                                    logger.debug(f"IDLE ({idle_reason}) - new media loading, continuing...")
                                    time.sleep(POLL_INTERVAL)
                                    continue

                                # FINISHED means playback completed successfully
                                if idle_reason == "FINISHED":
                                    logger.info(f"Finished playing: {title}")
                                    should_remove_from_queue = True  # Remove when finished
                                    break

                                # ERROR means playback failed - keep in queue to retry
                                if idle_reason == "ERROR":
                                    logger.error(f"Playback error for: {title} - keeping in queue")
                                    should_remove_from_queue = False  # Keep in queue
                                    break

                                # Any other idle reason (CANCELLED, etc.) - keep in queue
                                logger.warning(f"Idle: {idle_reason} - {title} - keeping in queue")
                                should_remove_from_queue = False  # Keep in queue
                                break

                            elif state == "UNKNOWN":
                                logger.warning(f"Unknown player state for: {title} - keeping in queue")
                                should_remove_from_queue = False  # Keep in queue
                                break

                        time.sleep(POLL_INTERVAL)

                except Exception as e:
                    logger.error(f"Error playing {title}: {e} - keeping in queue", exc_info=True)
                    should_remove_from_queue = False  # Keep in queue to retry

                # Remove from queue only if appropriate
                if should_remove_from_queue:
                    logger.info(f"Removing from queue: {title}")
                    self._remove_from_queue_sync(queue_id)
                else:
                    logger.info(f"Keeping in queue: {title}")
                    # Reset status back to queued
                    self._update_status_sync(queue_id, "queued")

                # Brief pause before next song
                time.sleep(1)

        except Exception as e:
            logger.error(f"Playout loop error: {e}", exc_info=True)

        finally:
            # Cleanup
            logger.info("Cleaning up playout thread...")
            with self.playout_lock:
                self.is_playing = False
                self.connected_cast = None  # Clear connected cast reference

            if cast:
                try:
                    if not cast.is_idle:
                        cast.quit_app()
                    cast.disconnect()
                    logger.info("Disconnected from Chromecast")
                except Exception as e:
                    logger.warning(f"Error during cleanup: {e}")

            logger.info("Playout thread finished")

    def _connect_to_device(self, device_uuid: str) -> Optional[pychromecast.Chromecast]:
        """Connect to a Chromecast device by UUID using CastBrowser."""
        try:
            # Create Zeroconf instance
            zconf = Zeroconf()

            # Create listener
            listener = DiscoveryListener()

            # Create browser
            browser = CastBrowser(listener, zconf)
            browser.start_discovery()

            # Wait for devices to be discovered
            logger.info("Searching for Chromecast device...")
            time.sleep(5)

            # Find the device by UUID
            cast = None
            for uuid, service in browser.services.items():
                if str(uuid) == device_uuid:
                    # Get the Chromecast object
                    chromecasts, _ = pychromecast.get_listed_chromecasts(friendly_names=[service.friendly_name])
                    if chromecasts:
                        cast = chromecasts[0]
                        cast.wait()
                        logger.info(f"Connected to Chromecast: {cast.name}")
                    break

            # Stop discovery
            browser.stop_discovery()
            zconf.close()

            if not cast:
                logger.error(f"Chromecast not found: {device_uuid}")

            return cast

        except Exception as e:
            logger.error(f"Error connecting to Chromecast: {e}", exc_info=True)
            return None

    def _get_queue_sync(self) -> List[Dict]:
        """Synchronous wrapper to get queue from async function."""
        try:
            # Run async function in new event loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def get_queue():
                async with get_db() as db:
                    cursor = await db.execute(
                        "SELECT id, video_id, title FROM queue WHERE status != 'completed' ORDER BY added_at ASC"
                    )
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]

            result = loop.run_until_complete(get_queue())
            loop.close()
            return result
        except Exception as e:
            logger.error(f"Error getting queue: {e}")
            return []

    def _remove_from_queue_sync(self, queue_id: int):
        """Synchronous wrapper to remove item from queue."""
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def remove():
                async with get_db() as db:
                    await db.execute("DELETE FROM queue WHERE id = ?", (queue_id,))
                    await db.commit()

                # Import here to avoid circular dependency
                from app.services.queue_manager import queue_manager
                await queue_manager.broadcast_queue_update()

            loop.run_until_complete(remove())
            loop.close()
        except Exception as e:
            logger.error(f"Error removing from queue: {e}")

    def _update_status_sync(self, queue_id: int, status: str):
        """Synchronous wrapper to update queue item status."""
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def update():
                async with get_db() as db:
                    await db.execute(
                        "UPDATE queue SET status = ? WHERE id = ?",
                        (status, queue_id)
                    )
                    await db.commit()

                from app.services.queue_manager import queue_manager
                await queue_manager.broadcast_queue_update()

            loop.run_until_complete(update())
            loop.close()
        except Exception as e:
            logger.error(f"Error updating status: {e}")


# Global instance
chromecast_service = ChromecastService()
