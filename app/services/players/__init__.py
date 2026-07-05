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
