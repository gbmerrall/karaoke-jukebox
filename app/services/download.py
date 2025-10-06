"""
Video download service using yt-dlp.
Downloads YouTube videos to local storage for Chromecast playback.
"""

import asyncio
import yt_dlp
from typing import Dict
from app.config import settings
import logging

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

    async def download(self, video_id: str, title: str = "") -> Dict[str, any]:
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
            DownloadError: If download fails
        """
        video_path = settings.get_video_path(video_id)

        # Check if already downloaded
        if self.is_downloaded(video_id):
            logger.info(f"Video already downloaded: {video_id} - {title}")
            return {
                "success": True,
                "video_path": str(video_path),
                "message": "Video already downloaded"
            }

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
            'format': (
                'bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/'  # H.264 + AAC
                'bestvideo[vcodec^=avc1]+bestaudio/'                     # H.264 + any audio
                'bestvideo[vcodec^=vp9][ext=webm]+bestaudio[ext=webm]/'  # VP9 + webm audio
                'bestvideo[vcodec^=vp09]+bestaudio/'                     # VP9 + any audio
                'bestvideo[vcodec!=av01][ext=mp4]+bestaudio/'            # Any non-AV1 MP4
                'bestvideo[vcodec!=av01]+bestaudio/'                     # Any non-AV1 video
                'best[vcodec!=av01]'                                     # Fallback: best non-AV1
            ),
            'outtmpl': str(self.videos_dir / f'{video_id}.%(ext)s'),
            'merge_output_format': 'mp4',
            'quiet': False,
            'no_warnings': False,
            'extract_flat': False,
            'ignoreerrors': False,
            'nocheckcertificate': False,
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
            logger.info(f"Download successful: {video_id} - {title} ({file_size_mb:.2f} MB)")

            return {
                "success": True,
                "video_path": str(video_path),
                "message": f"Downloaded successfully ({file_size_mb:.2f} MB)"
            }

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Download failed for {video_id}: {error_msg}")

            # Clean up partial download if exists
            if video_path.exists():
                try:
                    video_path.unlink()
                except Exception as cleanup_error:
                    logger.warning(f"Failed to clean up partial download: {cleanup_error}")

            # Provide user-friendly error messages
            if "ffmpeg" in error_msg.lower():
                user_message = "Server configuration error: ffmpeg is not installed. Please contact the administrator."
            elif "Video unavailable" in error_msg:
                user_message = "This video is unavailable or has been removed from YouTube"
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
