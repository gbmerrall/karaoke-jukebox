"""
Queue routes for SSE (Server-Sent Events) and queue management.
"""

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from app.routes.auth import get_session_user
from app.services.queue_manager import queue_manager
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/queue", tags=["Queue"])


@router.get("/sse")
async def queue_sse(request: Request):
    """
    Server-Sent Events endpoint for real-time queue updates.

    Clients connect to this endpoint and receive:
    - Initial queue state
    - Updates when queue changes
    - Heartbeat events every 30 seconds

    Usage with HTMX:
        <div hx-ext="sse" sse-connect="/queue/sse" sse-swap="queue-update">
    """
    # Get user session info
    username, is_admin = get_session_user(request)
    if username:
        logger.info(f"SSE connection from: {username} (admin: {is_admin})")
    else:
        logger.info("SSE connection from anonymous user")

    async def event_generator():
        """Generate SSE events."""
        try:
            async for event in queue_manager.subscribe(username, is_admin):
                yield event

        except Exception as e:
            logger.error(f"Error in SSE event generator: {e}")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        }
    )


@router.delete("/{queue_id}")
async def delete_queue_item(request: Request, queue_id: int):
    """
    Delete a queue item.

    Users can only delete their own items.
    Admins can delete any item.
    """
    username, is_admin = get_session_user(request)

    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        removed = await queue_manager.remove_from_queue(
            queue_id=queue_id,
            username=username,
            is_admin=is_admin
        )

        if removed:
            logger.info(f"Queue item {queue_id} removed by {username}")
            return JSONResponse(
                {"success": True, "message": "Item removed from queue"}
            )
        else:
            return JSONResponse(
                {"success": False, "message": "Item not found"},
                status_code=404
            )

    except PermissionError as e:
        logger.warning(f"Permission denied for {username} to remove queue item {queue_id}")
        raise HTTPException(status_code=403, detail=str(e))

    except Exception as e:
        logger.error(f"Error removing queue item: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to remove item from queue")
