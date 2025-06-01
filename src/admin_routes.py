from fastapi import APIRouter, Request, Form, Depends, BackgroundTasks, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from src import crud
from src.utils import get_session_user, templates, get_db
from src.dependencies import require_admin
import pychromecast
from pychromecast.controllers.youtube import YouTubeController
import threading
import time as pytime
import structlog
from src.db import SessionLocal
from pydantic import BaseModel, Field


logger = structlog.get_logger()

# Create admin router with dependency
router = APIRouter(
    prefix="/admin",
    dependencies=[Depends(require_admin)],
    tags=["Admin"]
)

# --- Admin API Models ---
class AdminConfigIn(BaseModel):
    username: str = Field(..., strip_whitespace=True, min_length=1)
    password: str = Field(..., min_length=1)

class AdminConfigOut(BaseModel):
    username: str
    class Config:
        from_attributes = True

# --- Admin API Endpoints ---
@router.post("/config", response_model=AdminConfigOut)
def set_admin_config(config: AdminConfigIn, db: Session = Depends(get_db)):
    """Set or update the admin username and password."""
    admin = crud.set_admin_config(db, username=config.username, password=config.password)
    if not admin:
        raise HTTPException(status_code=400, detail="Failed to set admin config.")
    return admin

@router.post("/login")
def admin_login(config: AdminConfigIn, db: Session = Depends(get_db)):
    """Verify admin login credentials."""
    valid = crud.verify_admin_password(db, username=config.username, password=config.password)
    if not valid:
        raise HTTPException(status_code=401, detail="Invalid admin credentials.")
    return {"message": "Login successful."}

discovered_chromecasts = []
selected_chromecast_uuid = None

# Global playout state
is_playing = False
playout_thread = None
playout_lock = threading.Lock()
skip_requested = threading.Event()


# Placeholder for synchronous notification from thread
def notify_queue_update_sync(db_session: Session | None = None): # db_session might not be needed
    logger.info("notify_queue_update_sync_called")
    # In a real implementation, this would signal the main async part of the app
    # to send a WebSocket update, possibly via a thread-safe queue.
    if db_session:
        # Example:
        # current_queue_length = len(crud.get_queue(db_session))
        # logger.debug("Current queue length for notification", length=current_queue_length)
        pass


def refresh_chromecasts():
    global discovered_chromecasts
    logger.info("refreshing_chromecasts")
    try:
        chromecasts, _ = pychromecast.get_chromecasts()
        discovered_chromecasts = [
            {"name": cc.cast_info.friendly_name, "uuid": str(cc.cast_info.uuid)}
            for cc in chromecasts
        ]
        logger.info("chromecasts_refreshed", count=len(discovered_chromecasts))
        return discovered_chromecasts
    except Exception as e:
        logger.error("chromecast_refresh_failed", error=str(e))
        raise


@router.get("/", response_class=HTMLResponse)
async def admin_page(
    request: Request,
    db: Session = Depends(get_db),
    admin_auth: tuple = Depends(require_admin)
):
    """Render the admin page."""
    user_name, is_admin = admin_auth
    logger.info("admin_page_accessed", user_name=user_name)
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "request": request,
            "user_name": user_name,
            "is_admin": is_admin,
            "queue": crud.get_queue(db),
        },
    )


@router.get("/admin/queue/partial", response_class=HTMLResponse)
def admin_queue_partial(request: Request, db: Session = Depends(get_db)):
    user_name, is_admin = get_session_user(request)
    if not user_name or not is_admin:
        return HTMLResponse(status_code=403, content="Unauthorized")
    queue_items = crud.get_queue(db)
    queue = []
    for item in queue_items:
        queue.append(
            {
                "id": item.id,
                "video_title": item.video.title if item.video else "",
                "video_id": item.video.youtube_id if item.video else "",
                "user_name": item.user.name if item.user else "",
                "added_at": item.added_at.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    return templates.TemplateResponse(
        request, "admin_queue_partial.html", {"queue": queue, "request": request}
    )


@router.get("/admin/devices/refresh")
def admin_devices_refresh(request: Request):
    user_name, is_admin = get_session_user(request)
    if not user_name or not is_admin:
        return JSONResponse({"success": False, "error": "Unauthorized"})
    try:
        devices = refresh_chromecasts()
        return JSONResponse({"success": True, "devices": devices})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


@router.post("/admin/device/select")
def admin_device_select(request: Request, device_id: str = Form(...)):
    user_name, is_admin = get_session_user(request)
    global selected_chromecast_uuid
    logger.info("device_selection_attempt", user_name=user_name, device_id=device_id)
    if not user_name or not is_admin:
        logger.warning("unauthorized_device_selection", user_name=user_name)
        if request.headers.get("hx-request") == "true":
            return templates.TemplateResponse(
                "admin_status_message.html",
                {"request": request, "error_message": "Unauthorized", "status_message": None},
            )
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    selected_chromecast_uuid = device_id if device_id else None
    msg = "Device selected" if device_id else "Device selection cleared"
    logger.info("device_selected", device_id=device_id, previous_device=selected_chromecast_uuid)
    if request.headers.get("hx-request") == "true":
        return templates.TemplateResponse(
            "admin_status_message.html",
            {"request": request, "status_message": msg, "error_message": None},
        )
    return RedirectResponse(
        url="/admin?status_message=Device+selected", status_code=status.HTTP_303_SEE_OTHER
    )


def _playout_loop(db_session_factory):
    """
    Background thread loop to manage playout of the queue to a Chromecast device.
    This function runs in a separate thread and handles fetching queue items,
    playing them on the selected Chromecast, removing them after playback,
    and responding to stop signals.
    """
    global is_playing, selected_chromecast_uuid, playout_lock, skip_requested
    
    db: Session = db_session_factory()
    cast = None
    current_cast_uuid = None
    yt_controller = None

    logger.info("Playout_thread_started")

    try:
        while True:
            with playout_lock:
                if not is_playing:
                    logger.info("Playout_loop: is_playing is false. Exiting loop.")
                    break
                
                local_selected_chromecast_uuid = selected_chromecast_uuid # Cache under lock

                if local_selected_chromecast_uuid is None:
                    logger.warning("Playout_loop: No Chromecast selected. Stopping playout.")
                    is_playing = False
                    break # Exit if no device is selected

            # Refresh queue
            current_queue = crud.get_queue(db)
            if not current_queue:
                logger.info("Playout_loop: Queue is empty. Stopping playout.")
                with playout_lock:
                    is_playing = False
                break # Exit if queue is empty

            # Chromecast connection management
            if cast is None or current_cast_uuid != local_selected_chromecast_uuid or not cast.socket_client.is_connected:
                if cast and cast.socket_client.is_connected:
                    cast.disconnect()
                current_cast_uuid = local_selected_chromecast_uuid
                logger.info("Playout_loop: Attempting to connect to Chromecast.", uuid=current_cast_uuid)
                cast = None
                yt_controller = None

                try:
                    chromecasts_list, _ = pychromecast.get_chromecasts(timeout=10)
                    cast_device_info = next((cc_info for cc_info in discovered_chromecasts if cc_info["uuid"] == current_cast_uuid), None)

                    if not cast_device_info:
                        logger.error("Playout_loop: Selected Chromecast UUID not in discovered list.", uuid=current_cast_uuid)
                        pytime.sleep(5)
                        continue

                    cast = next((c for c in chromecasts_list if str(c.uuid) == current_cast_uuid), None)

                    if cast:
                        cast.wait()
                        logger.info("Playout_loop: Chromecast connected.", device_name=cast.name)
                        yt_controller = YouTubeController()
                        cast.register_handler(yt_controller)
                        logger.info("Playout_loop: YouTubeController registered.")
                    else:
                        logger.error("Playout_loop: Selected Chromecast not found after scan.", uuid=current_cast_uuid)
                        refresh_chromecasts()
                        pytime.sleep(5)
                        continue
                except Exception as e:
                    logger.error("Playout_loop: Failed to connect to Chromecast.", error=str(e), exc_info=True)
                    cast = None
                    yt_controller = None
                    pytime.sleep(5)
                    continue
            
            # Safety check: If cast is connected but yt_controller is somehow None, re-initialize.
            if cast and cast.socket_client.is_connected and yt_controller is None:
                logger.warning("Playout_loop: yt_controller was None despite connected cast. Re-registering.")
                yt_controller = YouTubeController()
                cast.register_handler(yt_controller)

            # Double check is_playing before starting new track.
            with playout_lock:
                if not is_playing:
                    logger.info("Playout_loop: is_playing became false before starting new track. Exiting.")
                    break

            item_to_play = current_queue[0]
            video_id = item_to_play.video.youtube_id
            video_title = item_to_play.video.title
            logger.info("Playout_loop: Preparing to play video.", video_id=video_id, title=video_title)

            mc = cast.media_controller
            
            # With YouTubeController, it might manage its own state before playing.
            # Let's see if explicitly stopping previous media is needed or if yt.play_video handles it.
            # if mc.status and mc.status.player_state in ["PLAYING", "BUFFERING"]:
            #      logger.info("Playout_loop: Stopping previous media before playing next.")
            #      mc.stop()
            #      pytime.sleep(1) 

            logger.info("Playout_loop: Calling yt_controller.play_video.", video_id=video_id)
            try:
                if not yt_controller:
                    logger.error("Playout_loop: yt_controller is None, cannot play video.")
                    # This case should ideally be prevented by connection logic, but as a safeguard:
                    pytime.sleep(5) # Wait and hope connection logic re-establishes it
                    continue # Skip to next iteration to re-evaluate connection

                yt_controller.play_video(video_id)
                logger.info("Playout_loop: yt_controller.play_video call completed.")
            except Exception as e:
                logger.error("Playout_loop: Error calling yt_controller.play_video.", video_id=video_id, error=str(e), exc_info=True)
                # If play_video fails, remove item and try next, or stop.
                crud.remove_from_queue(db, item_to_play.id)
                db.commit()
                notify_queue_update_sync(db)
                pytime.sleep(2)
                continue

            pytime.sleep(1) # Give a moment for the command to propagate and state to update
            mc.block_until_active(timeout=15) 

            logger.info("Playout_loop: Post block_until_active (after YouTubeController).", current_media_status=str(mc.status))

            # If buffering, wait a bit longer for it to start playing
            if mc.status and mc.status.player_state == "BUFFERING":
                logger.info("Playout_loop: Media is BUFFERING, waiting for PLAYING state...")
                buffer_timeout_seconds = 10 # Wait up to 10 more seconds for playing to start
                buffer_start_time = pytime.time()
                while (pytime.time() - buffer_start_time) < buffer_timeout_seconds:
                    pytime.sleep(0.5) # Poll every 500ms
                    if not mc.status or mc.status.player_state == "PLAYING":
                        logger.info("Playout_loop: Media transitioned to PLAYING or status lost.", new_status=mc.status.player_state if mc.status else "No MC Status")
                        break
                    if mc.status.player_state == "IDLE" or mc.status.player_state == "ERROR":
                        logger.warning("Playout_loop: Media transitioned to IDLE/ERROR while waiting for PLAYING from BUFFERING.", new_status=mc.status.player_state)
                        break 
                    # Still buffering, continue loop
                else: # Loop finished without break (timeout)
                    logger.warning("Playout_loop: Timeout waiting for PLAYING state from BUFFERING.", final_status=mc.status.player_state if mc.status else "No MC Status")

            if not mc.status or mc.status.player_state not in ["PLAYING"]:
                logger.warning(
                    "Playout_loop: Media did not successfully transition to PLAYING state.", 
                    video_id=video_id, 
                    status=mc.status.player_state if mc.status else "No MC Status", 
                    full_mc_status=str(mc.status) if mc.status else "N/A"
                )
                crud.remove_from_queue(db, item_to_play.id)
                db.commit()
                notify_queue_update_sync(db)
                pytime.sleep(2) 
                continue

            # Successfully started playing
            expected_content_id = mc.status.content_id if mc.status else video_id # Fallback to our video_id if status is weird
            current_media_session_id = mc.status.media_session_id if mc.status else None # Still useful for logging
            logger.info("Playout_loop: Playback started.", 
                        video_id=video_id, 
                        title=video_title, 
                        expected_content_id=expected_content_id,
                        media_session_id=current_media_session_id)
            
            playback_started_time = pytime.time()
            MIN_PLAY_TIME_BEFORE_IDLE_CHECK = 5  # seconds - time before we consider IDLE or session change as end of this track
            MAX_SONG_DURATION = 20 * 60  # 20 minutes max per song

            media_playing = True
            while media_playing:
                with playout_lock:
                    if not is_playing: 
                        logger.info("Playout_loop: Stop signal received during playback.", video_id=video_id)
                        if mc.status and mc.status.player_state in ["PLAYING", "BUFFERING"]:
                            mc.stop()
                        media_playing = False
                        break 
                
                if skip_requested.is_set():
                    logger.info("Playout_loop: Skip signal detected.", video_id=video_id)
                    skip_requested.clear()
                    if mc.status and mc.status.player_state in ["PLAYING", "BUFFERING"]:
                        logger.info("Playout_loop: Stopping media due to skip request.")
                        mc.stop()
                    media_playing = False 
                    break

                pytime.sleep(2)

                if not cast.socket_client.is_connected:
                    logger.warning("Playout_loop: Chromecast disconnected during playback.", device_name=cast.name if cast else "N/A")
                    media_playing = False 
                    cast = None 
                    yt_controller = None 
                    break

                current_mc_status = mc.status 
                if current_mc_status:
                    current_player_state = current_mc_status.player_state
                    current_session_id = current_mc_status.media_session_id # For logging
                    actual_playing_content_id = current_mc_status.content_id
                    
                    # logger.debug(f"Playout_loop: Polling. State: {current_player_state}, Session: {current_session_id}, PlayingContentID: {actual_playing_content_id}, ExpectedContentID: {expected_content_id}")

                    has_played_minimum_time = (pytime.time() - playback_started_time) > MIN_PLAY_TIME_BEFORE_IDLE_CHECK

                    if current_player_state == "PLAYING":
                        if actual_playing_content_id != expected_content_id and has_played_minimum_time:
                            logger.info("Playout_loop: Different content_id detected while PLAYING. Assuming previous track ended.", 
                                        requested_video_id=video_id, 
                                        expected_content_id=expected_content_id, 
                                        new_content_id=actual_playing_content_id,
                                        session_id=current_session_id)
                            media_playing = False
                        elif current_mc_status.current_time == 0 and has_played_minimum_time and actual_playing_content_id == expected_content_id:
                            logger.info("Playout_loop: Player is PLAYING expected content_id but current_time is 0 after minimum play. Assuming track ended/restarted.", 
                                        video_id=video_id, content_id=actual_playing_content_id, session_id=current_session_id)
                            media_playing = False

                    elif current_player_state == "IDLE" and has_played_minimum_time:
                        logger.info("Playout_loop: Video finished or stopped (IDLE).", 
                                    video_id=video_id, 
                                    idle_reason=current_mc_status.idle_reason, 
                                    session_id=current_session_id)
                        media_playing = False
                    elif current_player_state in ["UNKNOWN", "ERROR"]:
                        logger.warning("Playout_loop: Video playback status UNKNOWN or ERROR.", 
                                     video_id=video_id, 
                                     status=current_player_state, 
                                     session_id=current_session_id)
                        media_playing = False
                else: 
                    logger.warning("Playout_loop: No media status available while polling.", video_id=video_id)
                    if (pytime.time() - playback_started_time) > (MIN_PLAY_TIME_BEFORE_IDLE_CHECK + 10): 
                        logger.warning("Playout_loop: No media status for extended period, assuming playback ended.", video_id=video_id)
                        media_playing = False
                
                if (pytime.time() - playback_started_time) > MAX_SONG_DURATION and media_playing:
                    logger.warning("Playout_loop: Song exceeded max duration, stopping current song.", video_id=video_id)
                    if mc.status and mc.status.player_state in ["PLAYING", "BUFFERING"]:
                        mc.stop()
                    media_playing = False
                
                if not media_playing: # If any condition above set media_playing to False, break from poll loop
                    break
            
            # Song finished, was skipped (by stop signal), errored out, or superseded by a new media session
            logger.info("Playout_loop: Exited media_playing loop.", video_id=video_id, final_player_state=current_mc_status.player_state if current_mc_status else "N/A")

            with playout_lock:
                still_playing_globally = is_playing 

            if still_playing_globally:
                # Verify item is still at the head of the queue before removing
                # This is to avoid issues if 'skip' removed it already
                current_queue_after_play = crud.get_queue(db)
                if current_queue_after_play and current_queue_after_play[0].id == item_to_play.id:
                    logger.info("Playout_loop: Removing played item from queue.", item_id=item_to_play.id)
                    crud.remove_from_queue(db, item_to_play.id)
                    db.commit()
                elif not current_queue_after_play or current_queue_after_play[0].id != item_to_play.id:
                    logger.info("Playout_loop: Item was likely removed by skip or queue cleared. Not removing again.", item_id=item_to_play.id)

                notify_queue_update_sync(db) # Notify after potential removal
            
            # Brief pause before the next song or loop exit
            pytime.sleep(1)

    except Exception as e:
        logger.error("Playout_loop: Unhandled exception in playout loop.", error=str(e), exc_info=True)
    finally:
        logger.info("Playout_thread_finishing.")
        with playout_lock:
            is_playing = False # Ensure flag is reset
        if cast and cast.socket_client.is_connected:
            if cast.media_controller.status and cast.media_controller.status.player_state in ["PLAYING", "BUFFERING"]:
                logger.info("Playout_loop: Stopping media controller in finally block.")
                cast.media_controller.stop()
            logger.info("Playout_loop: Disconnecting Chromecast in finally block.")
            cast.disconnect()
        db.close() # Close the thread-specific database session
        logger.info("Playout_thread_finished_and_cleaned_up.")
        notify_queue_update_sync() # Final notification that playout has fully stopped.


@router.post("/admin/playout/start")
def admin_playout_start(
    request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
):
    """
    Start playing from the video queue.
    Initiates playback on the selected Chromecast device, starting with the
    first item in the queue and proceeding sequentially.
    """
    global is_playing, playout_thread, selected_chromecast_uuid, playout_lock
    user_name, is_admin = get_session_user(request)
    logger.info("admin_playout_start_request", user_name=user_name, is_admin=is_admin)
    if not user_name or not is_admin:
        logger.warning("unauthorized_playout_start_attempt", user_name=user_name)
        if request.headers.get("hx-request") == "true":
            return templates.TemplateResponse(
                "admin_status_message.html",
                {"request": request, "error_message": "Unauthorized to start playout", "status_message": None},
            )
        return RedirectResponse(url="/?error_message=Unauthorized", status_code=status.HTTP_302_FOUND)

    msg_to_send = None
    error_msg_to_send = None

    with playout_lock:
        if is_playing:
            error_msg_to_send = "Playout is already active."
            logger.info("playout_start_already_active", current_status=is_playing)
        elif selected_chromecast_uuid is None:
            error_msg_to_send = "No Chromecast selected. Please select a device first."
            logger.warning("playout_start_no_device_selected")
        else:
            queue_items = crud.get_queue(db)
            if not queue_items:
                error_msg_to_send = "Queue is empty. Add songs to the queue to start playout."
                logger.info("playout_start_queue_empty")
            else:
                is_playing = True
                playout_thread = threading.Thread(target=_playout_loop, args=(SessionLocal,), daemon=True)
                playout_thread.start()
                msg_to_send = "Playout started."
                logger.info("playout_started_successfully", device_uuid=selected_chromecast_uuid)

    if msg_to_send:
        notify_queue_update_sync(db) # Notify only on successful start

    if request.headers.get("hx-request") == "true":
        return templates.TemplateResponse(
            "admin_status_message.html",
            {"request": request, "status_message": msg_to_send, "error_message": error_msg_to_send}
        )
    
    redirect_url = "/admin"
    if msg_to_send:
        redirect_url += f"?status_message={msg_to_send.replace(' ', '+')}"
    elif error_msg_to_send:
        redirect_url += f"?error_message={error_msg_to_send.replace(' ', '+')}"
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/playout/skip")
def admin_playout_skip(
    request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
):
    """
    Skip the current playing song and proceed to the next.
    Removes the current track from the queue and signals the Chromecast
    to stop current playback, allowing the playout loop to pick the next song.
    """
    global is_playing, selected_chromecast_uuid, playout_lock, skip_requested
    user_name, is_admin = get_session_user(request)
    logger.info("admin_playout_skip_request", user_name=user_name, is_admin=is_admin)
    if not user_name or not is_admin:
        logger.warning("unauthorized_playout_skip_attempt", user_name=user_name)
        if request.headers.get("hx-request") == "true":
            return templates.TemplateResponse(
                "admin_status_message.html",
                {"request": request, "error_message": "Unauthorized to skip track", "status_message": None},
            )
        return RedirectResponse(url="/?error_message=Unauthorized", status_code=status.HTTP_302_FOUND)

    msg_to_send = None
    error_msg_to_send = None

    with playout_lock:
        if not is_playing:
            error_msg_to_send = "Playout is not active. Nothing to skip."
            logger.info("playout_skip_not_active")
        else:
            queue_items = crud.get_queue(db)
            if not queue_items:
                error_msg_to_send = "Queue is empty. Nothing to skip."
                logger.info("playout_skip_queue_empty")
            else:
                first_item = queue_items[0]
                logger.info("playout_skip_attempting", item_id=first_item.id, title=first_item.video.title if first_item.video else "N/A")
                crud.remove_from_queue(db, first_item.id)
                db.commit()
                logger.info("playout_skip_removed_from_db", item_id=first_item.id)
                skip_requested.set()
                logger.info("playout_skip_signal_sent_to_loop")
                msg_to_send = f"Skipped: {first_item.video.title if first_item.video else 'Unknown Track'}"
                notify_queue_update_sync(db)

    if request.headers.get("hx-request") == "true":
        return templates.TemplateResponse(
            "admin_status_message.html",
            {"request": request, "status_message": msg_to_send, "error_message": error_msg_to_send}
        )
    
    redirect_url = "/admin"
    if msg_to_send:
        redirect_url += f"?status_message={msg_to_send.replace(' ', '+')}"
    elif error_msg_to_send:
        redirect_url += f"?error_message={error_msg_to_send.replace(' ', '+')}"
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/playout/stop")
def admin_playout_stop(request: Request, background_tasks: BackgroundTasks):
    """
    Stop playing from the video queue.
    Signals the playback loop to terminate and stops any currently
    playing media on the Chromecast.
    """    
    global is_playing, playout_thread, playout_lock, selected_chromecast_uuid
    user_name, is_admin = get_session_user(request)
    logger.info("admin_playout_stop_request", user_name=user_name, is_admin=is_admin)
    if not user_name or not is_admin:
        logger.warning("unauthorized_playout_stop_attempt", user_name=user_name)
        if request.headers.get("hx-request") == "true":
            return templates.TemplateResponse(
                "admin_status_message.html",
                {"request": request, "error_message": "Unauthorized to stop playout", "status_message": None},
            )
        return RedirectResponse(url="/?error_message=Unauthorized", status_code=status.HTTP_302_FOUND)

    msg_to_send = None
    error_msg_to_send = None # Though stop doesn't usually have errors unless already stopped
    changed_state = False

    with playout_lock:
        if is_playing:
            is_playing = False 
            changed_state = True
            logger.info("playout_stop_signal_sent_to_loop")
            
            # Attempt to stop media on Chromecast directly as well
            # The loop's finally block will also try to do this.
            cast = None
            if selected_chromecast_uuid:
                try:
                    chromecasts_list, _ = pychromecast.get_chromecasts(timeout=5)
                    cast = next((c for c in chromecasts_list if str(c.uuid) == selected_chromecast_uuid), None)
                    if cast:
                        cast.wait()
                        mc = cast.media_controller
                        if mc.status and mc.status.player_state in ["PLAYING", "BUFFERING"]:
                            mc.stop()
                            logger.info("playout_stop_directly_stopped_media", device_uuid=selected_chromecast_uuid)
                except Exception as e:
                    logger.error("playout_stop_error_stopping_media", error=str(e), exc_info=True)
                finally:
                    if cast and cast.socket_client.is_connected:
                        cast.disconnect()
            
            # We don't join the playout_thread here to keep the request responsive.
            # The thread is a daemon and will exit. _playout_loop has cleanup.
            # playout_thread = None # Cleared when loop actually finishes if needed
        else:
            error_msg_to_send = "Playout is not currently active."
            logger.info("playout_stop_already_not_active")

    if changed_state:
        notify_queue_update_sync()

    if request.headers.get("hx-request") == "true":
        return templates.TemplateResponse(
            "admin_status_message.html", 
            {"request": request, "status_message": msg_to_send, "error_message": error_msg_to_send}
        )

    redirect_url = "/admin"
    if msg_to_send:
        redirect_url += f"?status_message={msg_to_send.replace(' ', '+')}"
    elif error_msg_to_send:
        redirect_url += f"?error_message={error_msg_to_send.replace(' ', '+')}"
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/queue/clear")
def admin_queue_clear(
    request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
):
    """
    Clear all tracks from the current video queue.
    If playout is active, it will stop once the queue is detected as empty by the playout loop.
    """
    user_name, is_admin = get_session_user(request)
    logger.info("admin_queue_clear_request", user_name=user_name, is_admin=is_admin)
    if not user_name or not is_admin:
        logger.warning("unauthorized_queue_clear_attempt", user_name=user_name)
        if request.headers.get("hx-request") == "true":
            return templates.TemplateResponse(
                "admin_status_message.html",
                {"request": request, "error_message": "Unauthorized to clear queue", "status_message": None},
            )
        return RedirectResponse(url="/?error_message=Unauthorized", status_code=status.HTTP_302_FOUND)
    
    try:
        crud.clear_queue(db)
        db.commit()
        msg = "Queue cleared successfully."
        logger.info("queue_cleared_successfully", user_name=user_name)
        
        notify_queue_update_sync(db) 

        if request.headers.get("hx-request") == "true":
            # Return only the status message. The queue will be updated by SSE.
            return templates.TemplateResponse(
                 "admin_status_message.html", 
                 {"request": request, "status_message": msg, "error_message": None}
            )

        return RedirectResponse(url=f"/admin?status_message={msg}", status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        db.rollback()
        logger.error("queue_clear_failed", error=str(e), user_name=user_name, exc_info=True)
        msg = "Failed to clear queue."
        if request.headers.get("hx-request") == "true":
            return templates.TemplateResponse(
                "admin_status_message.html",
                {"request": request, "error_message": msg, "status_message": None},
            )
        return RedirectResponse(url=f"/admin?error_message={msg}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/queue/clear", response_class=HTMLResponse)
async def clear_queue(
    request: Request,
    db: Session = Depends(get_db),
    admin_auth: tuple = Depends(require_admin)
):
    """Clear the entire queue."""
    user_name, is_admin = admin_auth
    logger.info("clear_queue_called", user_name=user_name)
    crud.clear_queue(db)
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "request": request,
            "user_name": user_name,
            "is_admin": is_admin,
            "queue": crud.get_queue(db),
        },
    )


@router.post("/queue/delete/{queue_id}", response_class=HTMLResponse)
async def delete_queue_item(
    request: Request,
    queue_id: int,
    db: Session = Depends(get_db),
    admin_auth: tuple = Depends(require_admin)
):
    """Delete a specific queue item."""
    user_name, is_admin = admin_auth
    logger.info("delete_queue_item_called", user_name=user_name, queue_id=queue_id)
    crud.remove_from_queue(db, queue_id)
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "request": request,
            "user_name": user_name,
            "is_admin": is_admin,
            "queue": crud.get_queue(db),
        },
    )
