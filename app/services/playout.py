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
from typing import Dict, List, Optional, Tuple

# project imports
from app.config import settings
from app.database import get_db
from app.services.players import PlaybackOutcome, Player
from app.services.players.factory import create_player

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

    def startup(self) -> None:
        """Acquire the backend's app-lifetime resources (lifespan startup hook)."""
        self.player.startup()

    async def discover_devices(self, timeout: int = 10) -> List[Dict]:
        """Scan for output devices via the backend.

        Args:
            timeout: Scan timeout in seconds.

        Returns:
            Device dicts from the backend; [] for backends without discovery.
        """
        if not self.player.supports_discovery:
            return []
        # keep_connection is a snapshot of is_playing, not a live re-check. Safe
        # because all routes that mutate is_playing run on this same event loop
        # and there is no await between this read and the backend's disconnect
        # decision; do not introduce one without moving the check into the backend.
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

    def start_playback(self) -> Dict:
        """Start playback from the queue.

        Returns:
            Dict with 'success' and 'message' keys.
        """
        with self.playout_lock:
            if self.is_playing:
                return {"success": False, "message": "Playback is already active"}

            if self.player.supports_discovery and not self.player.selected_device_uuid:
                return {"success": False, "message": "No playback device selected"}

            self.is_playing = True
            self.stop_requested.clear()
            self.skip_requested.clear()

            self.playout_thread = threading.Thread(
                target=self._playout_loop, daemon=True
            )
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

                next_up_text = None
                if len(queue) > 1:
                    next_item = queue[1]
                    next_up_text = (
                        f"Up next: {next_item['title']} — for {next_item['username']}"
                    )

                logger.info(f"Playing: {title}")
                self._update_status_sync(queue_id, "playing")

                try:
                    outcome = self.player.play(
                        item["video_id"],
                        self.skip_requested,
                        self.stop_requested,
                        next_up_text,
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

    def _apply_outcome(
        self, queue_id: int, title: str, outcome: PlaybackOutcome
    ) -> None:
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
                        "SELECT id, video_id, title, username FROM queue "
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


# Global instance. The backend comes from PLAYER_BACKEND; a future runtime
# toggle would rebuild this via the same factory.
playout_service = PlayoutService(create_player(settings.player_backend))
