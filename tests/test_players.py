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
