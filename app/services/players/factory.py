"""
Playback backend factory.

Resolves the PLAYER_BACKEND setting to a Player instance with lazy imports, so
the Chromecast path never imports python-mpv and the mpv path never imports
pychromecast. This function is the seam a future runtime backend toggle would
plug into.
"""

# standard library
import logging

# project imports
from app.services.players import Player

logger = logging.getLogger(__name__)


def create_player(backend: str) -> Player:
    """Instantiate the configured playback backend.

    Args:
        backend: Normalized backend name ('chromecast' or 'mpv').

    Returns:
        The backend Player instance (not yet started up).

    Raises:
        ValueError: If the backend name is unknown. The settings validator
            rejects unknown values at startup; this is defense in depth.
    """
    if backend == "chromecast":
        from app.services.players.chromecast_player import ChromecastPlayer

        logger.info("Playback backend: chromecast")
        return ChromecastPlayer()
    if backend == "mpv":
        from app.services.players.mpv_player import MpvPlayer

        logger.info("Playback backend: mpv")
        return MpvPlayer()
    raise ValueError(f"Unknown PLAYER_BACKEND: {backend}")
