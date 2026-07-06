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


def test_stub_player_satisfies_protocol():
    """A structural (duck-typed) backend passes the runtime protocol check."""
    assert isinstance(_StubPlayer(), Player)


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
