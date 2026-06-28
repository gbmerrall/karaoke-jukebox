"""
Input validation helpers.

Kept deliberately small and dependency-free so any module can import it
without risking circular imports.
"""

import re

# A YouTube video ID is always exactly 11 characters from this alphabet.
# Validating against this prevents a crafted `video_id` from being used to
# build filesystem paths or inject extra query parameters into the watch URL.
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def is_valid_video_id(video_id: str) -> bool:
    """Return True if `video_id` is a well-formed YouTube video ID.

    Args:
        video_id: The candidate video ID (e.g. from a URL path parameter).

    Returns:
        True if it matches the canonical 11-char YouTube ID format.
    """
    return bool(video_id) and bool(_VIDEO_ID_RE.match(video_id))
