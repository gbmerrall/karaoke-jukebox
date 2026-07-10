"""
Search and queue management routes.
Handles YouTube search, video queueing, and the main app page.
"""

import logging

from fastapi import APIRouter, Request, Form, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.rate_limit import RateLimiter
from app.routes.auth import require_session, get_session_user
from app.services.download import download_service, DownloadError
from app.services.queue_manager import queue_manager
from app.services.youtube import youtube_service, YouTubeError
from app.validators import is_valid_video_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Search & Queue"])

templates = Jinja2Templates(directory="app/templates")

# Per-user rate limits. Keyed by username (falling back to client IP for
# anonymous callers). Search guards YouTube API quota; queue guards downloads.
_search_limiter = RateLimiter(max_events=30, window_seconds=60)
_queue_limiter = RateLimiter(max_events=20, window_seconds=60)


def _rate_limit_key(request: Request, username: str | None = None) -> str:
    """Build a rate-limit key from the username or the client IP."""
    if username:
        return f"user:{username}"
    client = request.client.host if request.client else "unknown"
    return f"ip:{client}"


@router.get("/app", response_class=HTMLResponse)
async def app_page(request: Request, user_data: tuple = Depends(require_session)):
    """
    Render the main application page.
    Requires valid session.
    """
    username, is_admin = user_data

    # Get current queue
    queue = await queue_manager.get_queue()

    logger.debug(f"App page queue: {len(queue)} items for {username}")

    return templates.TemplateResponse(
        request,
        "app.html",
        {
            "username": username,
            "is_admin": is_admin,
            "queue": queue,
        },
    )


@router.get("/search", response_class=HTMLResponse)
async def search_form(request: Request):
    """Return the search form partial (for HTMX)."""
    return templates.TemplateResponse(request, "partials/search_form.html", {})


@router.post("/search", response_class=HTMLResponse)
async def search(request: Request, query: str = Form(...)):
    """
    Search YouTube for videos.
    Returns search results partial for HTMX swap.
    """
    username, is_admin = get_session_user(request)

    if not query.strip():
        return templates.TemplateResponse(
            request,
            "partials/search_results.html",
            {"results": [], "error": "Please enter a search query"},
        )

    if not _search_limiter.allow(_rate_limit_key(request, username)):
        return templates.TemplateResponse(
            request,
            "partials/search_results.html",
            {
                "results": [],
                "error": "Too many searches. Please slow down and try again shortly.",
            },
        )

    try:
        # Search YouTube (will append 'karaoke' automatically)
        results = await youtube_service.search(query.strip())

        logger.info(
            f"Search by {username}: '{query.strip()} karaoke' - {len(results)} results"
        )

        return templates.TemplateResponse(
            request,
            "partials/search_results.html",
            {
                "results": results,
                "username": username,
                "is_admin": is_admin,
                "error": None,
            },
        )

    except YouTubeError as e:
        return templates.TemplateResponse(
            request,
            "partials/search_results.html",
            {"results": [], "error": e.user_message},
        )
    except Exception as e:
        logger.error(f"Search error: {e}")
        return templates.TemplateResponse(
            request,
            "partials/search_results.html",
            {
                "results": [],
                "error": "Search failed. Please try again.",
            },
        )


@router.post("/queue/{video_id}", response_class=HTMLResponse)
async def queue_video(
    request: Request,
    video_id: str,
    background_tasks: BackgroundTasks,
    title: str = Form(...),
    thumbnail_url: str = Form(...),
    duration: int = Form(...),
    views: int = Form(...),
    owner: str = Form(""),
):
    """
    Queue a video for playback.

    If video not already downloaded, triggers background download.
    Adds to queue immediately and notifies user.
    """
    username, is_admin = get_session_user(request)

    if not username:
        return templates.TemplateResponse(
            request,
            "partials/modals.html",
            {
                "modal_type": "error",
                "message": "You must be logged in to queue videos",
            },
        )

    effective_username = username
    if is_admin:
        owner = owner.strip()
        if not owner:
            return templates.TemplateResponse(
                request,
                "partials/modals.html",
                {
                    "modal_type": "error",
                    "message": "Enter who this song is for.",
                },
            )
        effective_username = owner

    # Reject malformed video IDs before they touch the filesystem or yt-dlp.
    if not is_valid_video_id(video_id):
        return templates.TemplateResponse(
            request,
            "partials/modals.html",
            {"modal_type": "error", "message": "Invalid video."},
        )

    # Throttle queueing per user to prevent download/disk/bandwidth abuse.
    if not _queue_limiter.allow(_rate_limit_key(request, username)):
        return templates.TemplateResponse(
            request,
            "partials/modals.html",
            {
                "modal_type": "error",
                "message": "You're queueing too fast. Please wait a moment.",
            },
        )

    try:
        # Check if already downloaded
        is_downloaded = download_service.is_downloaded(video_id)

        if not is_downloaded:
            # Trigger background download
            logger.info(f"Starting download for {video_id}: {title}")
            background_tasks.add_task(
                download_video_and_queue,
                video_id,
                title,
                thumbnail_url,
                duration,
                views,
                effective_username,
            )

            return templates.TemplateResponse(
                request,
                "partials/modals.html",
                {
                    "modal_type": "info",
                    "message": f"Downloading '{title}'... It will be added to the queue shortly.",
                },
            )
        else:
            # Already downloaded, add to queue immediately
            await queue_manager.add_to_queue(
                video_id=video_id,
                title=title,
                thumbnail_url=thumbnail_url,
                duration=duration,
                views=views,
                username=effective_username,
            )

            logger.info(f"Queued (already downloaded): {title} by {effective_username}")

            return templates.TemplateResponse(
                request,
                "partials/modals.html",
                {
                    "modal_type": "success",
                    "message": f"'{title}' added to queue!",
                },
            )

    except ValueError as e:
        # Queue validation error (duplicate, queue full, etc.)
        logger.warning(f"Queue validation error: {e}")
        return templates.TemplateResponse(
            request,
            "partials/modals.html",
            {"modal_type": "error", "message": str(e)},
        )
    except Exception as e:
        logger.error(f"Error queueing video: {e}", exc_info=True)
        return templates.TemplateResponse(
            request,
            "partials/modals.html",
            {
                "modal_type": "error",
                "message": "Failed to queue video. Please try again.",
            },
        )


async def download_video_and_queue(
    video_id: str,
    title: str,
    thumbnail_url: str,
    duration: int,
    views: int,
    username: str,
):
    """
    Background task to download video and add to queue.

    This runs asynchronously after the response is sent to the user.
    """
    try:
        # Download the video
        result = await download_service.download(video_id, title)

        if result["success"]:
            # Add to queue
            await queue_manager.add_to_queue(
                video_id=video_id,
                title=title,
                thumbnail_url=thumbnail_url,
                duration=duration,
                views=views,
                username=username,
            )
            logger.info(f"Downloaded and queued: {title} by {username}")
        else:
            logger.error(f"Download failed for {video_id}: {result.get('error')}")

    except DownloadError as e:
        logger.error(f"Download error for {video_id}: {e}")
        # Note: User already got the "downloading..." message
        # In a more advanced version, we could send a notification via SSE
        # For now, they'll notice when it doesn't appear in the queue

    except ValueError as e:
        # Queue validation error
        logger.warning(f"Queue validation error after download: {e}")

    except Exception as e:
        logger.error(
            f"Unexpected error in download_video_and_queue: {e}", exc_info=True
        )
