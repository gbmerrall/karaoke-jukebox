"""
Integration test for the yt-dlp video download pipeline.

The download path is the single most fragile part of this app: yt-dlp must keep
up with YouTube's frequent changes, so it needs a real end-to-end check rather
than a mock. This test downloads a known-good public video and verifies that the
result is a real, playable, Chromecast-compatible MP4 (not just a non-empty
file).

It is an OPT-IN integration test: it hits the live network and runs the real
yt-dlp + ffmpeg toolchain. Run it explicitly with:

    uv run pytest -m integration --run-integration

A passing run is the canary that tells you the YouTube download flow still works
after a yt-dlp/YouTube change. A failure almost always means "bump yt-dlp".
"""

import json
import shutil
import subprocess

import pytest

from app.config import settings
from app.services.download import DownloadError, VideoDownloadService
from tests.conftest import has_internet

# "Never Gonna Give You Up" - a stable, public, non-age-restricted video that is
# extremely unlikely to be removed. The reliable canary clip. IYKYK.
KNOWN_GOOD_VIDEO_ID = "dQw4w9WgXcQ"

# A real download of this clip is several MB even at low quality; anything under
# this is a sign of a partial/broken download rather than a real video.
MIN_EXPECTED_BYTES = 500_000

# Codecs Chromecast cannot play. The download format string in download.py
# explicitly excludes AV1, so we assert the result honours that.
UNSUPPORTED_VIDEO_CODECS = {"av01", "av1"}


def ffprobe_streams(path: str) -> dict:
    """Run ffprobe on `path` and return its parsed JSON (format + streams).

    Args:
        path: Filesystem path to the media file to inspect.

    Returns:
        Parsed ffprobe JSON with top-level "format" and "streams" keys.
    """
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.fixture
def isolated_videos_dir(tmp_path, monkeypatch):
    """Point the download service at a throwaway data dir for the test.

    `settings.get_videos_dir()` / `get_video_path()` derive from
    `settings.data_dir`, so overriding that single field redirects all writes
    into pytest's tmp_path and keeps the real ./data/videos untouched.
    """
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path


@pytest.mark.integration
async def test_download_known_good_video_produces_playable_mp4(isolated_videos_dir):
    """The canary: downloading dQw4w9WgXcQ yields a valid, playable MP4.

    Verifies the full real pipeline (yt-dlp format selection -> ffmpeg merge ->
    file on disk) end to end. If this fails, the YouTube download flow is broken
    for real users - the usual fix is updating yt-dlp.
    """
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe not installed (required for downloads)")
    if not has_internet():
        pytest.skip("no internet connectivity to YouTube")

    # Fresh instance so it reads the monkeypatched videos dir (the module-level
    # singleton captured the real dir at import time).
    service = VideoDownloadService()

    try:
        result = await service.download(KNOWN_GOOD_VIDEO_ID, title="canary clip")
    except DownloadError as e:
        pytest.fail(
            f"Download failed for {KNOWN_GOOD_VIDEO_ID}: {e}. "
            "This usually means yt-dlp needs updating "
            "(make preflight, or uv sync --upgrade-package yt-dlp) to keep up with YouTube."
        )
        return  # unreachable (pytest.fail raises); keeps static analysis happy

    # 1. Service reports success and points at a real, non-trivial file.
    assert result["success"] is True
    video_path = settings.get_video_path(KNOWN_GOOD_VIDEO_ID)
    assert video_path.exists(), "download reported success but file is missing"
    size = video_path.stat().st_size
    assert size > MIN_EXPECTED_BYTES, (
        f"downloaded file is suspiciously small ({size} bytes) - likely a "
        "partial or broken download"
    )

    # 2. is_downloaded() agrees (the app uses this to skip re-downloads).
    assert service.is_downloaded(KNOWN_GOOD_VIDEO_ID) is True

    # 3. ffprobe confirms it is a real, playable, Chromecast-compatible MP4.
    probe = ffprobe_streams(str(video_path))
    assert "mp4" in probe["format"]["format_name"], (
        f"unexpected container: {probe['format']['format_name']}"
    )

    video_streams = [s for s in probe["streams"] if s.get("codec_type") == "video"]
    audio_streams = [s for s in probe["streams"] if s.get("codec_type") == "audio"]
    assert video_streams, "file has no video stream"
    assert audio_streams, "file has no audio stream (karaoke without sound is useless)"

    vcodec = video_streams[0].get("codec_name", "").lower()
    assert vcodec not in UNSUPPORTED_VIDEO_CODECS, (
        f"video codec {vcodec!r} is not Chromecast-compatible; the format "
        "selection in download.py should have excluded it"
    )


async def test_download_is_idempotent_when_already_present(isolated_videos_dir):
    """download() short-circuits when the clip is already on disk.

    Guards the is_downloaded() fast path in download() so the app does not
    re-hit YouTube for a clip it already has. This is pure filesystem logic:
    is_downloaded() only checks that the target file exists and is non-empty,
    so the precondition is established by writing a dummy file rather than doing
    a real (redundant) YouTube fetch. The live yt-dlp/YouTube extraction is
    covered by test_download_known_good_video_produces_playable_mp4; repeating a
    real download here would add no extraction coverage and only double the
    rate-limit exposure that makes the suite flaky in CI.
    """
    service = VideoDownloadService()

    # Establish the "already downloaded" precondition without touching the
    # network: is_downloaded() is satisfied by any non-empty file at the path.
    video_path = settings.get_video_path(KNOWN_GOOD_VIDEO_ID)
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"not a real mp4, just enough to look present")
    assert service.is_downloaded(KNOWN_GOOD_VIDEO_ID) is True

    # download() must see the existing file and return the cached-file result
    # instead of invoking yt-dlp.
    result = await service.download(KNOWN_GOOD_VIDEO_ID, title="canary clip")
    assert result["success"] is True
    assert result["message"] == "Video already downloaded"
    assert result["video_path"] == str(video_path)
