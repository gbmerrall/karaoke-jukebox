"""
YouTube search service using google-api-python-client.
Searches for karaoke videos and returns metadata.
"""

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from typing import List, Dict
from app.config import settings
import isodate
import asyncio
import logging

logger = logging.getLogger(__name__)


class YouTubeError(Exception):
    """Search failed. `user_message` is safe to show to end users."""

    def __init__(self, user_message: str):
        """Store a user-facing message alongside the exception."""
        super().__init__(user_message)
        self.user_message = user_message


class YouTubeService:
    """Service for searching YouTube videos."""

    def __init__(self):
        """Initialize YouTube API client."""
        self.youtube = build("youtube", "v3", developerKey=settings.youtube_api_key)

    async def search(self, query: str, max_results: int = 20) -> List[Dict]:
        """
        Search for karaoke videos on YouTube.

        Args:
            query: User's search query (will have 'karaoke' appended)
            max_results: Maximum number of results to return (default: 20)

        Returns:
            List of video dictionaries with metadata:
                - video_id: YouTube video ID
                - title: Video title
                - thumbnail_url: URL to video thumbnail
                - duration: Video duration in seconds
                - views: View count

        Raises:
            Exception: If YouTube API request fails
        """
        # Append 'karaoke' to the search query
        search_query = f"{query} karaoke"

        logger.info(f"Searching YouTube for: {search_query}")

        try:
            # Search for videos (run in thread pool as API is synchronous)
            search_response = await asyncio.to_thread(
                lambda: self.youtube.search()
                .list(
                    q=search_query,
                    part="id,snippet",
                    type="video",
                    maxResults=max_results,
                    order="relevance",  # Order by relevance (best match)
                    videoCategoryId="10",  # Music category
                )
                .execute()
            )

            video_ids = [
                item["id"]["videoId"] for item in search_response.get("items", [])
            ]

            if not video_ids:
                logger.info(f"No videos found for query: {search_query}")
                return []

            # Get detailed video statistics and content details
            videos_response = await asyncio.to_thread(
                lambda: self.youtube.videos()
                .list(id=",".join(video_ids), part="snippet,contentDetails,statistics")
                .execute()
            )

            results = []
            for item in videos_response.get("items", []):
                try:
                    # Parse ISO 8601 duration to seconds
                    duration_iso = item["contentDetails"]["duration"]
                    duration_seconds = int(
                        isodate.parse_duration(duration_iso).total_seconds()
                    )

                    # Get view count
                    view_count = int(item["statistics"].get("viewCount", 0))

                    # Get thumbnail (prefer high quality)
                    thumbnails = item["snippet"]["thumbnails"]
                    thumbnail_url = (
                        thumbnails.get("high", {}).get("url")
                        or thumbnails.get("medium", {}).get("url")
                        or thumbnails.get("default", {}).get("url", "")
                    )

                    results.append(
                        {
                            "video_id": item["id"],
                            "title": item["snippet"]["title"],
                            "thumbnail_url": thumbnail_url,
                            "duration": duration_seconds,
                            "views": view_count,
                        }
                    )
                except (KeyError, ValueError) as e:
                    logger.warning(
                        f"Error parsing video data for {item.get('id')}: {e}"
                    )
                    continue

            # Results are already ordered by relevance from YouTube API
            logger.info(f"Found {len(results)} videos for query: {search_query}")
            return results

        except HttpError as e:
            status = getattr(getattr(e, "resp", None), "status", None)
            detail = str(e)
            logger.error(f"YouTube API error (status={status}): {detail}")

            if status == 403 and "quotaExceeded" in detail:
                raise YouTubeError(
                    "YouTube search is temporarily unavailable (daily quota "
                    "exceeded). Please try again later."
                )
            if status in (400, 403) and (
                "keyInvalid" in detail or "API key not valid" in detail
            ):
                raise YouTubeError(
                    "YouTube search is misconfigured (invalid API key). "
                    "Please contact the administrator."
                )
            raise YouTubeError("YouTube search failed. Please try again.")
        except Exception as e:
            logger.error(f"Unexpected error during YouTube search: {e}")
            raise YouTubeError("YouTube search failed. Please try again.")


# Global instance
youtube_service = YouTubeService()
