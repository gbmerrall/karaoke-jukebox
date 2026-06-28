"""Unit tests for video ID validation."""

import pytest

from app.validators import is_valid_video_id


@pytest.mark.parametrize(
    "video_id",
    [
        "dQw4w9WgXcQ",  # canonical
        "_-aBcDeFgH1",  # underscores and dashes are allowed
        "00000000000",
    ],
)
def test_valid_ids(video_id):
    """Well-formed 11-char YouTube IDs are accepted."""
    assert is_valid_video_id(video_id) is True


@pytest.mark.parametrize(
    "video_id",
    [
        "",  # empty
        "short",  # too short
        "dQw4w9WgXcQX",  # too long (12)
        "dQw4w9WgXc",  # too short (10)
        "abc&list=PLxyz",  # query-param injection attempt
        "../../etc/passwd",  # path traversal attempt
        "dQw4 9WgXcQ",  # space
        "dQw4w9WgXc.",  # disallowed punctuation
    ],
)
def test_invalid_ids(video_id):
    """Malformed, too-short/long, or injection-y IDs are rejected."""
    assert is_valid_video_id(video_id) is False
