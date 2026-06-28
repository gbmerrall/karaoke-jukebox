"""
Unit tests for the YouTube search and video download services.

Both services normally hit the network (YouTube Data API / yt-dlp), so every
test here mocks the external boundary:

- YouTubeService: `app.services.youtube.build` is patched so no discovery
  document is fetched, and the two `.execute()` calls return canned dicts.
- VideoDownloadService: `_download_sync` (or the module-level `yt_dlp`) is
  patched so nothing is ever downloaded.

The suite is fully offline and needs no secrets beyond the dummy env vars that
conftest.py already exports before the app is imported.
"""

from unittest.mock import MagicMock

import pytest

from googleapiclient.errors import HttpError

from app.services.download import DownloadError, VideoDownloadService
from app.services.youtube import YouTubeError, YouTubeService

VALID_VIDEO_ID = "dQw4w9WgXcQ"


# ---------------------------------------------------------------------------
# YouTube service helpers
# ---------------------------------------------------------------------------
def make_youtube_service(
    monkeypatch, search_response=None, videos_response=None, execute_error=None
):
    """Build a YouTubeService whose API client is a MagicMock.

    Patches `app.services.youtube.build` so the constructor does not fetch a
    discovery document, then wires the search/videos `.execute()` calls to
    return the supplied canned dicts (or raise `execute_error`).

    Args:
        monkeypatch: pytest monkeypatch fixture.
        search_response: Dict returned by `youtube.search().list().execute()`.
        videos_response: Dict returned by `youtube.videos().list().execute()`.
        execute_error: Exception to raise from both `.execute()` calls instead.

    Returns:
        A YouTubeService instance backed by the configured MagicMock client.
    """
    client = MagicMock()
    if execute_error is not None:
        client.search.return_value.list.return_value.execute.side_effect = execute_error
        client.videos.return_value.list.return_value.execute.side_effect = execute_error
    else:
        client.search.return_value.list.return_value.execute.return_value = (
            search_response or {}
        )
        client.videos.return_value.list.return_value.execute.return_value = (
            videos_response or {}
        )

    monkeypatch.setattr("app.services.youtube.build", lambda *a, **k: client)
    return YouTubeService()


def make_video_item(
    video_id, title="Song karaoke", duration="PT3M30S", views="1000", thumbnails=None
):
    """Return a fake videos.list item dict in YouTube Data API shape.

    Args:
        video_id: The video id string (videos.list returns id as a string).
        title: Snippet title.
        duration: ISO 8601 contentDetails duration.
        views: statistics.viewCount as a string.
        thumbnails: Snippet thumbnails dict; defaults to a full high/medium/default set.

    Returns:
        A dict shaped like one element of a videos.list response.
    """
    if thumbnails is None:
        thumbnails = {
            "high": {"url": "https://img/high.jpg"},
            "medium": {"url": "https://img/medium.jpg"},
            "default": {"url": "https://img/default.jpg"},
        }
    return {
        "id": video_id,
        "snippet": {"title": title, "thumbnails": thumbnails},
        "contentDetails": {"duration": duration},
        "statistics": {"viewCount": views},
    }


def make_http_error(status, content_text):
    """Build a googleapiclient HttpError with a controllable status and body.

    Args:
        status: The HTTP status code exposed via `error.resp.status`.
        content_text: Text embedded in the body so it appears in `str(error)`.

    Returns:
        An HttpError instance.
    """
    resp = MagicMock()
    resp.status = status
    resp.reason = content_text
    content = (
        '{"error": {"code": %d, "message": "%s", "errors": [{"reason": "%s"}]}}'
        % (status, content_text, content_text)
    ).encode("utf-8")
    return HttpError(resp, content)


# ---------------------------------------------------------------------------
# YouTube service tests
# ---------------------------------------------------------------------------
async def test_search_parses_multiple_items(monkeypatch):
    """A normal search parses id/title/thumbnail/duration/views for each item."""
    search_response = {
        "items": [
            {"id": {"videoId": "aaaaaaaaaaa"}},
            {"id": {"videoId": "bbbbbbbbbbb"}},
        ]
    }
    videos_response = {
        "items": [
            make_video_item(
                "aaaaaaaaaaa", title="First karaoke", duration="PT4M", views="2500"
            ),
            make_video_item(
                "bbbbbbbbbbb", title="Second karaoke", duration="PT1M5S", views="42"
            ),
        ]
    }
    service = make_youtube_service(
        monkeypatch, search_response=search_response, videos_response=videos_response
    )

    results = await service.search("test query")

    assert len(results) == 2
    first = results[0]
    assert first["video_id"] == "aaaaaaaaaaa"
    assert first["title"] == "First karaoke"
    assert first["thumbnail_url"] == "https://img/high.jpg"
    assert first["duration"] == 240
    assert first["views"] == 2500
    assert results[1]["duration"] == 65
    assert results[1]["views"] == 42


async def test_search_thumbnail_preference(monkeypatch):
    """Thumbnail selection prefers high, then medium, then default."""
    search_response = {
        "items": [
            {"id": {"videoId": "aaaaaaaaaaa"}},
            {"id": {"videoId": "bbbbbbbbbbb"}},
            {"id": {"videoId": "ccccccccccc"}},
        ]
    }
    videos_response = {
        "items": [
            make_video_item(
                "aaaaaaaaaaa",
                thumbnails={
                    "medium": {"url": "https://img/medium.jpg"},
                    "default": {"url": "https://img/default.jpg"},
                },
            ),
            make_video_item(
                "bbbbbbbbbbb",
                thumbnails={"default": {"url": "https://img/default.jpg"}},
            ),
            make_video_item(
                "ccccccccccc",
                thumbnails={
                    "high": {"url": "https://img/high.jpg"},
                    "medium": {"url": "https://img/medium.jpg"},
                },
            ),
        ]
    }
    service = make_youtube_service(
        monkeypatch, search_response=search_response, videos_response=videos_response
    )

    results = await service.search("test")

    assert results[0]["thumbnail_url"] == "https://img/medium.jpg"
    assert results[1]["thumbnail_url"] == "https://img/default.jpg"
    assert results[2]["thumbnail_url"] == "https://img/high.jpg"


async def test_search_empty_results_returns_empty_list(monkeypatch):
    """No search items short-circuits to an empty list without a videos call."""
    service = make_youtube_service(monkeypatch, search_response={"items": []})

    results = await service.search("nothing here")

    assert results == []
    service.youtube.videos.assert_not_called()


async def test_search_skips_item_missing_keys(monkeypatch):
    """An item missing contentDetails/statistics is skipped, others returned."""
    search_response = {
        "items": [
            {"id": {"videoId": "aaaaaaaaaaa"}},
            {"id": {"videoId": "bbbbbbbbbbb"}},
        ]
    }
    broken = {
        "id": "aaaaaaaaaaa",
        "snippet": {"title": "Broken", "thumbnails": {}},
        # contentDetails / statistics deliberately omitted -> KeyError -> skip
    }
    videos_response = {
        "items": [broken, make_video_item("bbbbbbbbbbb", title="Good karaoke")]
    }
    service = make_youtube_service(
        monkeypatch, search_response=search_response, videos_response=videos_response
    )

    results = await service.search("test")

    assert len(results) == 1
    assert results[0]["video_id"] == "bbbbbbbbbbb"


async def test_search_quota_exceeded_message(monkeypatch):
    """A 403 quotaExceeded HttpError maps to the quota user_message."""
    error = make_http_error(403, "quotaExceeded")
    service = make_youtube_service(monkeypatch, execute_error=error)

    with pytest.raises(YouTubeError) as exc_info:
        await service.search("test")

    assert exc_info.value.user_message == (
        "YouTube search is temporarily unavailable (daily quota "
        "exceeded). Please try again later."
    )


async def test_search_key_invalid_message(monkeypatch):
    """A 400 keyInvalid HttpError maps to the misconfiguration user_message."""
    error = make_http_error(400, "keyInvalid")
    service = make_youtube_service(monkeypatch, execute_error=error)

    with pytest.raises(YouTubeError) as exc_info:
        await service.search("test")

    assert exc_info.value.user_message == (
        "YouTube search is misconfigured (invalid API key). "
        "Please contact the administrator."
    )


async def test_search_http_error_generic_fallback(monkeypatch):
    """An unrecognized HttpError falls back to the generic user_message."""
    error = make_http_error(500, "internalError")
    service = make_youtube_service(monkeypatch, execute_error=error)

    with pytest.raises(YouTubeError) as exc_info:
        await service.search("test")

    assert exc_info.value.user_message == "YouTube search failed. Please try again."


async def test_search_non_http_exception_generic(monkeypatch):
    """A non-HttpError exception is wrapped in the generic YouTubeError."""
    service = make_youtube_service(monkeypatch, execute_error=RuntimeError("boom"))

    with pytest.raises(YouTubeError) as exc_info:
        await service.search("test")

    assert exc_info.value.user_message == "YouTube search failed. Please try again."


# ---------------------------------------------------------------------------
# Download service tests
# ---------------------------------------------------------------------------
def write_file(path, content=b"x"):
    """Create `path` (and parents) with the given bytes.

    Args:
        path: Destination Path.
        content: Bytes to write (empty bytes => zero-size file).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_is_downloaded_true_for_nonempty_file(initialized_db):
    """is_downloaded is True only when the file exists and is non-empty."""
    from app.config import settings

    service = VideoDownloadService()
    write_file(settings.get_video_path(VALID_VIDEO_ID), b"real video bytes")

    assert service.is_downloaded(VALID_VIDEO_ID) is True


def test_is_downloaded_false_for_empty_file(initialized_db):
    """A zero-byte file does not count as downloaded."""
    from app.config import settings

    service = VideoDownloadService()
    write_file(settings.get_video_path(VALID_VIDEO_ID), b"")

    assert service.is_downloaded(VALID_VIDEO_ID) is False


def test_is_downloaded_false_for_missing_file(initialized_db):
    """A missing file is not downloaded."""
    service = VideoDownloadService()

    assert service.is_downloaded(VALID_VIDEO_ID) is False


async def test_download_invalid_id_raises(initialized_db):
    """An invalid video id is rejected before touching the filesystem."""
    service = VideoDownloadService()

    with pytest.raises(DownloadError) as exc_info:
        await service.download("../etc")

    assert str(exc_info.value) == "Invalid video ID"


async def test_download_idempotent_fast_path(initialized_db, monkeypatch):
    """A pre-existing file short-circuits without invoking yt_dlp."""
    from app.config import settings

    service = VideoDownloadService()
    write_file(settings.get_video_path(VALID_VIDEO_ID), b"already here")

    mock_yt = MagicMock()
    monkeypatch.setattr("app.services.download.yt_dlp", mock_yt)

    result = await service.download(VALID_VIDEO_ID, title="cached")

    assert result["success"] is True
    assert result["message"] == "Video already downloaded"
    mock_yt.YoutubeDL.assert_not_called()


async def test_download_happy_path(initialized_db, monkeypatch):
    """A successful _download_sync produces a file and a success result."""
    from app.config import settings

    service = VideoDownloadService()
    video_path = settings.get_video_path(VALID_VIDEO_ID)

    def fake_download_sync(video_id, ydl_opts):
        """Simulate yt-dlp by writing a non-empty output file."""
        write_file(settings.get_video_path(video_id), b"x" * 2048)

    monkeypatch.setattr(service, "_download_sync", fake_download_sync)

    result = await service.download(VALID_VIDEO_ID, title="new song")

    assert result["success"] is True
    assert "Downloaded successfully" in result["message"]
    assert video_path.exists()


async def test_download_failure_ffmpeg_message(initialized_db, monkeypatch):
    """A ffmpeg error maps to the ffmpeg user_message and cleans up partials."""
    from app.config import settings

    service = VideoDownloadService()
    video_path = settings.get_video_path(VALID_VIDEO_ID)

    def fake_download_sync(video_id, ydl_opts):
        """Write a partial file, then fail as if ffmpeg were missing."""
        write_file(settings.get_video_path(video_id), b"partial")
        raise RuntimeError("ffmpeg not found on PATH")

    monkeypatch.setattr(service, "_download_sync", fake_download_sync)

    with pytest.raises(DownloadError) as exc_info:
        await service.download(VALID_VIDEO_ID)

    assert str(exc_info.value) == (
        "Server configuration error: ffmpeg is not installed. "
        "Please contact the administrator."
    )
    assert not video_path.exists()


async def test_download_failure_video_unavailable_message(initialized_db, monkeypatch):
    """A 'Video unavailable' error maps to its dedicated user_message."""
    service = VideoDownloadService()

    def fake_download_sync(video_id, ydl_opts):
        """Fail as if YouTube reported the video as unavailable."""
        raise RuntimeError("ERROR: Video unavailable")

    monkeypatch.setattr(service, "_download_sync", fake_download_sync)

    with pytest.raises(DownloadError) as exc_info:
        await service.download(VALID_VIDEO_ID)

    assert str(exc_info.value) == (
        "This video is unavailable or has been removed from YouTube"
    )


async def test_download_succeeds_but_no_file_raises(initialized_db, monkeypatch):
    """If _download_sync produces no file, download reports the verify failure."""
    service = VideoDownloadService()

    def fake_download_sync(video_id, ydl_opts):
        """Pretend to succeed without ever writing an output file."""
        return None

    monkeypatch.setattr(service, "_download_sync", fake_download_sync)

    # The internal "file not found" DownloadError is raised inside the try block,
    # so it is caught by the outer handler and remapped to the generic message.
    with pytest.raises(DownloadError) as exc_info:
        await service.download(VALID_VIDEO_ID)

    assert str(exc_info.value) == "Failed to download video. Please try another video."


@pytest.mark.parametrize(
    "raised_text, expected_message",
    [
        ("Private video, sign in", "This video is private and cannot be downloaded"),
        ("HTTP Error 403: Forbidden", "Access to this video is forbidden"),
        ("No space left on disk", "Insufficient disk space to download video"),
        (
            "some unexpected failure",
            "Failed to download video. Please try another video.",
        ),
    ],
)
async def test_download_failure_message_branches(
    initialized_db, monkeypatch, raised_text, expected_message
):
    """Each recognised error substring maps to its specific user_message."""
    service = VideoDownloadService()

    def fake_download_sync(video_id, ydl_opts):
        """Fail with the parametrized error text."""
        raise RuntimeError(raised_text)

    monkeypatch.setattr(service, "_download_sync", fake_download_sync)

    with pytest.raises(DownloadError) as exc_info:
        await service.download(VALID_VIDEO_ID)

    assert str(exc_info.value) == expected_message


def test_download_sync_invokes_yt_dlp(initialized_db, monkeypatch):
    """_download_sync builds the watch URL and drives yt_dlp.YoutubeDL."""
    service = VideoDownloadService()

    captured = {}
    ydl_context = MagicMock()
    ydl_instance = ydl_context.__enter__.return_value

    def fake_youtubedl(opts):
        """Record opts and return a context manager that records the URL."""
        captured["opts"] = opts
        return ydl_context

    monkeypatch.setattr("app.services.download.yt_dlp.YoutubeDL", fake_youtubedl)

    service._download_sync(VALID_VIDEO_ID, {"format": "best"})

    ydl_instance.download.assert_called_once_with(
        [f"https://www.youtube.com/watch?v={VALID_VIDEO_ID}"]
    )
    assert captured["opts"] == {"format": "best"}


async def test_get_video_lock_identity(initialized_db):
    """Same id yields the same lock; different ids yield different locks."""
    service = VideoDownloadService()

    lock_a1 = await service._get_video_lock("aaaaaaaaaaa")
    lock_a2 = await service._get_video_lock("aaaaaaaaaaa")
    lock_b = await service._get_video_lock("bbbbbbbbbbb")

    assert lock_a1 is lock_a2
    assert lock_a1 is not lock_b
