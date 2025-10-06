"""
Search and queue management routes.
Handles YouTube search, video queueing, and the main app page.
"""

from fastapi import APIRouter, Request, Form, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from app.routes.auth import require_session, get_session_user
from app.services.youtube import youtube_service
from app.services.download import download_service, DownloadError
from app.services.queue_manager import queue_manager
import logging

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Search & Queue"])

templates = Jinja2Templates(directory="app/templates")


@router.get("/app", response_class=HTMLResponse)
async def app_page(request: Request, user_data: tuple = Depends(require_session)):
    """
    Render the main application page.
    Requires valid session.
    """
    username, is_admin = user_data

    # Get current queue
    queue = await queue_manager.get_queue()

    # Debug: log queue data
    logger.info(f"App page queue: {len(queue)} items")
    if queue:
        logger.info(f"First item keys: {queue[0].keys() if queue else 'none'}")
        logger.info(f"First item: {queue[0] if queue else 'none'}")

    return templates.TemplateResponse(
        "app.html",
        {
            "request": request,
            "username": username,
            "is_admin": is_admin,
            "queue": queue,
        }
    )


@router.get("/search", response_class=HTMLResponse)
async def search_form(request: Request):
    """Return the search form partial (for HTMX)."""
    return templates.TemplateResponse(
        "partials/search_form.html",
        {"request": request}
    )


@router.post("/search", response_class=HTMLResponse)
async def search(request: Request, query: str = Form(...)):
    """
    Search YouTube for videos.
    Returns search results partial for HTMX swap.
    """
    username, is_admin = get_session_user(request)

    if not query.strip():
        return templates.TemplateResponse(
            "partials/search_results.html",
            {
                "request": request,
                "results": [],
                "error": "Please enter a search query"
            }
        )

    try:
        # Search YouTube (will append 'karaoke' automatically)
        results = await youtube_service.search(query.strip())

        logger.info(f"Search by {username}: '{query.strip()} karaoke' - {len(results)} results")

        return templates.TemplateResponse(
            "partials/search_results.html",
            {
                "request": request,
                "results": results,
                "username": username,
                "error": None
            }
        )

    except Exception as e:
        logger.error(f"Search error: {e}")
        return templates.TemplateResponse(
            "partials/search_results.html",
            {
                "request": request,
                "results": [],
                "error": "Search failed. Please try again."
            }
        )


@router.post("/queue/{video_id}", response_class=HTMLResponse)
async def queue_video(
    request: Request,
    video_id: str,
    background_tasks: BackgroundTasks,
    title: str = Form(...),
    thumbnail_url: str = Form(...),
    duration: int = Form(...),
    views: int = Form(...)
):
    """
    Queue a video for playback.

    If video not already downloaded, triggers background download.
    Adds to queue immediately and notifies user.
    """
    username, is_admin = get_session_user(request)

    if not username:
        return templates.TemplateResponse(
            "partials/modals.html",
            {
                "request": request,
                "modal_type": "error",
                "message": "You must be logged in to queue videos"
            }
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
                username
            )

            return templates.TemplateResponse(
                "partials/modals.html",
                {
                    "request": request,
                    "modal_type": "info",
                    "message": f"Downloading '{title}'... It will be added to the queue shortly."
                }
            )
        else:
            # Already downloaded, add to queue immediately
            await queue_manager.add_to_queue(
                video_id=video_id,
                title=title,
                thumbnail_url=thumbnail_url,
                duration=duration,
                views=views,
                username=username
            )

            logger.info(f"Queued (already downloaded): {title} by {username}")

            return templates.TemplateResponse(
                "partials/modals.html",
                {
                    "request": request,
                    "modal_type": "success",
                    "message": f"'{title}' added to queue!"
                }
            )

    except ValueError as e:
        # Queue validation error (duplicate, queue full, etc.)
        logger.warning(f"Queue validation error: {e}")
        return templates.TemplateResponse(
            "partials/modals.html",
            {
                "request": request,
                "modal_type": "error",
                "message": str(e)
            }
        )
    except Exception as e:
        logger.error(f"Error queueing video: {e}", exc_info=True)
        return templates.TemplateResponse(
            "partials/modals.html",
            {
                "request": request,
                "modal_type": "error",
                "message": "Failed to queue video. Please try again."
            }
        )


async def download_video_and_queue(
    video_id: str,
    title: str,
    thumbnail_url: str,
    duration: int,
    views: int,
    username: str
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
                username=username
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
        logger.error(f"Unexpected error in download_video_and_queue: {e}", exc_info=True)
