"""
Video download service using yt-dlp.
Downloads YouTube videos to local storage for Chromecast playback.
"""

import asyncio
import logging
from typing import Any, Dict

import yt_dlp

from app.config import settings
from app.validators import is_valid_video_id

logger = logging.getLogger(__name__)


class DownloadError(Exception):
    """Raised when video download fails."""

    pass


class VideoDownloadService:
    """Service for downloading YouTube videos using yt-dlp."""

    def __init__(self):
        """Initialize the download service."""
        self.videos_dir = settings.get_videos_dir()
        # Ensure videos directory exists
        self.videos_dir.mkdir(parents=True, exist_ok=True)
        # Per-video locks so two callers cannot download the same video_id
        # concurrently into the same output file. The guard protects the dict.
        self._video_locks: Dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _get_video_lock(self, video_id: str) -> asyncio.Lock:
        """Return a per-video_id asyncio.Lock, creating it on first use."""
        async with self._locks_guard:
            lock = self._video_locks.get(video_id)
            if lock is None:
                lock = asyncio.Lock()
                self._video_locks[video_id] = lock
            return lock

    def is_downloaded(self, video_id: str) -> bool:
        """
        Check if a video has already been downloaded.

        Args:
            video_id: YouTube video ID

        Returns:
            True if the video file exists, False otherwise
        """
        video_path = settings.get_video_path(video_id)
        return video_path.exists() and video_path.stat().st_size > 0

    async def download(self, video_id: str, title: str = "") -> Dict[str, Any]:
        """
        Download a YouTube video asynchronously.

        Args:
            video_id: YouTube video ID
            title: Video title (for logging)

        Returns:
            Dict with download result:
                - success: bool
                - video_path: Path to downloaded file (if successful)
                - error: Error message (if failed)

        Raises:
            DownloadError: If the video_id is invalid or the download fails.
        """
        # Reject anything that is not a canonical YouTube ID before it reaches
        # the filesystem path or the watch URL.
        if not is_valid_video_id(video_id):
            raise DownloadError("Invalid video ID")

        video_path = settings.get_video_path(video_id)

        # Serialize downloads of the same video so two near-simultaneous queue
        # requests do not both spawn yt-dlp writing the same output file.
        lock = await self._get_video_lock(video_id)
        async with lock:
            # Re-check inside the lock: the other caller may have just finished.
            if self.is_downloaded(video_id):
                logger.info(f"Video already downloaded: {video_id} - {title}")
                return {
                    "success": True,
                    "video_path": str(video_path),
                    "message": "Video already downloaded",
                }

            return await self._download_locked(video_id, title, video_path)

    async def _download_locked(
        self, video_id: str, title: str, video_path
    ) -> Dict[str, Any]:
        """Run the actual download. Caller must hold the per-video lock."""
        logger.info(f"Starting download: {video_id} - {title}")

        # yt-dlp options for Chromecast-compatible video
        # Chromecast supports: H.264 (avc1), VP8, VP9
        # Chromecast does NOT support: AV1 (av01)
        ydl_opts = {
            # Format selection:
            # 1. Prefer H.264 (avc1) video with AAC audio
            # 2. Fallback to VP9/VP8 with compatible audio
            # 3. Explicitly exclude AV1 codec (vcodec!=av01)
            # 4. Merge to MP4 container
            "format": (
                "bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"  # H.264 + AAC
                "bestvideo[vcodec^=avc1]+bestaudio/"  # H.264 + any audio
                "bestvideo[vcodec^=vp9][ext=webm]+bestaudio[ext=webm]/"  # VP9 + webm audio
                "bestvideo[vcodec^=vp09]+bestaudio/"  # VP9 + any audio
                "bestvideo[vcodec!=av01][ext=mp4]+bestaudio/"  # Any non-AV1 MP4
                "bestvideo[vcodec!=av01]+bestaudio/"  # Any non-AV1 video
                "best[vcodec!=av01]"  # Fallback: best non-AV1
            ),
            "outtmpl": str(self.videos_dir / f"{video_id}.%(ext)s"),
            "merge_output_format": "mp4",
            "quiet": False,
            "no_warnings": False,
            "extract_flat": False,
            "ignoreerrors": False,
            "nocheckcertificate": False,
            # Progress hooks could be added here for future UI updates
            # 'progress_hooks': [self._progress_hook],
        }

        try:
            # Run yt-dlp in thread pool to avoid blocking
            await asyncio.to_thread(self._download_sync, video_id, ydl_opts)

            # Verify download succeeded
            if not self.is_downloaded(video_id):
                raise DownloadError("Download completed but file not found")

            file_size_mb = video_path.stat().st_size / (1024 * 1024)
            logger.info(
                f"Download successful: {video_id} - {title} ({file_size_mb:.2f} MB)"
            )

            return {
                "success": True,
                "video_path": str(video_path),
                "message": f"Downloaded successfully ({file_size_mb:.2f} MB)",
            }

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Download failed for {video_id}: {error_msg}")

            # Clean up partial download if exists
            if video_path.exists():
                try:
                    video_path.unlink()
                except Exception as cleanup_error:
                    logger.warning(
                        f"Failed to clean up partial download: {cleanup_error}"
                    )

            # Provide user-friendly error messages
            if "ffmpeg" in error_msg.lower():
                user_message = "Server configuration error: ffmpeg is not installed. Please contact the administrator."
            elif "Video unavailable" in error_msg:
                user_message = (
                    "This video is unavailable or has been removed from YouTube"
                )
            elif "Private video" in error_msg:
                user_message = "This video is private and cannot be downloaded"
            elif "403" in error_msg or "Forbidden" in error_msg:
                user_message = "Access to this video is forbidden"
            elif "disk" in error_msg.lower() or "space" in error_msg.lower():
                user_message = "Insufficient disk space to download video"
            else:
                user_message = "Failed to download video. Please try another video."

            raise DownloadError(user_message)

    def _download_sync(self, video_id: str, ydl_opts: Dict) -> None:
        """
        Synchronous download using yt-dlp.
        Called via asyncio.to_thread to avoid blocking.

        Args:
            video_id: YouTube video ID
            ydl_opts: yt-dlp options dictionary

        Raises:
            Exception: If download fails
        """
        url = f"https://www.youtube.com/watch?v={video_id}"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])


# Global instance
download_service = VideoDownloadService()
