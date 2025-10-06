"""
Admin routes for Chromecast control and queue management.
All routes require admin authentication.
"""

from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from app.routes.auth import require_admin
from app.services.chromecast import chromecast_service
from app.services.queue_manager import queue_manager
import logging

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin",
    tags=["Admin"],
    dependencies=[Depends(require_admin)]
)

templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
async def admin_page(request: Request):
    """
    Render the admin control panel.
    Requires admin authentication.
    """
    username, is_admin = require_admin(request)

    # Get initial queue state for page load
    # (SSE will then keep it updated in real-time)
    queue = await queue_manager.get_queue()

    logger.info(f"Admin page accessed by {username}")

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "username": username,
            "is_admin": is_admin,
            "queue": queue,
        }
    )


@router.get("/devices/scan")
async def scan_devices(request: Request):
    """
    Scan for available Chromecast devices on the network.

    Returns:
        JSON with list of discovered devices
    """
    username, _ = require_admin(request)
    logger.info(f"Device scan initiated by {username}")

    try:
        devices = await chromecast_service.discover_devices(timeout=10)

        return JSONResponse({
            "success": True,
            "devices": devices,
            "count": len(devices)
        })

    except Exception as e:
        logger.error(f"Device scan failed: {e}", exc_info=True)
        return JSONResponse({
            "success": False,
            "error": "Failed to scan for devices. Please try again.",
            "devices": []
        })


@router.post("/devices/select")
async def select_device(request: Request, device_uuid: str = Form(...)):
    """
    Select a Chromecast device for playback.

    Args:
        device_uuid: UUID of the device to select
    """
    username, _ = require_admin(request)
    logger.info(f"Device selection by {username}: {device_uuid}")

    success = chromecast_service.select_device(device_uuid)

    if success:
        return JSONResponse({
            "success": True,
            "message": "Device selected successfully"
        })
    else:
        return JSONResponse({
            "success": False,
            "message": "Device not found"
        }, status_code=404)


@router.post("/playback/start")
async def start_playback(request: Request):
    """
    Start playback from the queue on the selected Chromecast device.

    Requires:
    - Chromecast device to be selected
    - Queue to have at least one item
    """
    username, _ = require_admin(request)
    logger.info(f"Playback start requested by {username}")

    # Check if queue has items
    queue_size = await queue_manager.get_queue_size()
    if queue_size == 0:
        return JSONResponse({
            "success": False,
            "message": "Queue is empty. Add songs before starting playback."
        }, status_code=400)

    result = chromecast_service.start_playback()

    return JSONResponse(result)


@router.post("/playback/stop")
async def stop_playback(request: Request):
    """
    Stop playback on the Chromecast device.
    """
    username, _ = require_admin(request)
    logger.info(f"Playback stop requested by {username}")

    result = chromecast_service.stop_playback()

    return JSONResponse(result)


@router.post("/playback/skip")
async def skip_current(request: Request):
    """
    Skip the currently playing song and advance to the next.
    """
    username, _ = require_admin(request)
    logger.info(f"Skip requested by {username}")

    result = chromecast_service.skip_current()

    return JSONResponse(result)


@router.get("/status")
async def get_status(request: Request):
    """
    Get current playback status.

    Returns:
        JSON with playback state, selected device, and queue info
    """
    is_playing = chromecast_service.is_playing
    selected_device = chromecast_service.selected_device_uuid
    queue_size = await queue_manager.get_queue_size()
    currently_playing = await queue_manager.get_currently_playing()

    return JSONResponse({
        "is_playing": is_playing,
        "selected_device_uuid": selected_device,
        "queue_size": queue_size,
        "currently_playing": currently_playing
    })


@router.delete("/queue/{queue_id}")
async def admin_delete_queue_item(request: Request, queue_id: int):
    """
    Admin can delete any queue item (no ownership check).

    Args:
        queue_id: ID of queue item to delete
    """
    username, _ = require_admin(request)
    logger.info(f"Admin queue delete by {username}: item {queue_id}")

    try:
        removed = await queue_manager.remove_from_queue(
            queue_id=queue_id,
            is_admin=True
        )

        if removed:
            return JSONResponse({
                "success": True,
                "message": "Item removed from queue"
            })
        else:
            return JSONResponse({
                "success": False,
                "message": "Item not found"
            }, status_code=404)

    except Exception as e:
        logger.error(f"Error removing queue item: {e}", exc_info=True)
        return JSONResponse({
            "success": False,
            "message": "Failed to remove item"
        }, status_code=500)


@router.post("/queue/clear")
async def clear_queue(request: Request):
    """
    Clear all items from the queue.
    """
    username, _ = require_admin(request)
    logger.info(f"Queue clear requested by {username}")

    try:
        # Get all queue items and remove them
        queue = await queue_manager.get_queue()

        for item in queue:
            await queue_manager.remove_from_queue(
                queue_id=item["id"],
                is_admin=True
            )

        return JSONResponse({
            "success": True,
            "message": f"Cleared {len(queue)} items from queue"
        })

    except Exception as e:
        logger.error(f"Error clearing queue: {e}", exc_info=True)
        return JSONResponse({
            "success": False,
            "message": "Failed to clear queue"
        }, status_code=500)
