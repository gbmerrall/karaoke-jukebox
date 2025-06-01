from fastapi import APIRouter, Request, Form, Depends, BackgroundTasks, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session
from src import crud
from src.models import Queue
import os
from src.youtube_utils import download_youtube_video
from src.utils import get_session_user, notify_queue_update, templates, get_db, queue_update_event_generator
from pydantic import BaseModel
from typing import List

router = APIRouter()

# --- Queue API Models ---
class QueueAdd(BaseModel):
    user_id: int
    video_id: int

class QueueOut(BaseModel):
    id: int
    video_id: int
    user_id: int
    added_at: str
    class Config:
        from_attributes = True

# --- Queue API Endpoints ---
@router.post("/api/queue", response_model=QueueOut, status_code=status.HTTP_201_CREATED)
def add_to_queue(item: QueueAdd, db: Session = Depends(get_db)):
    """Add a video to the queue for a user."""
    queue_item = crud.add_to_queue(db, user_id=item.user_id, video_id=item.video_id)
    if not queue_item:
        raise HTTPException(status_code=400, detail="Failed to add to queue.")
    return queue_item

@router.get("/api/queue", response_model=List[QueueOut])
def get_queue(db: Session = Depends(get_db)):
    """Get the current queue (FIFO order)."""
    return crud.get_queue(db)

@router.delete("/api/queue/{queue_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_from_queue(queue_id: int, db: Session = Depends(get_db)):
    """Remove a queue item by its ID."""
    success = crud.remove_from_queue(db, queue_id)
    if not success:
        raise HTTPException(status_code=404, detail="Queue item not found.")
    return

@router.get("/queue", response_class=HTMLResponse)
def queue_page(request: Request, db: Session = Depends(get_db)):
    user_name, is_admin = get_session_user(request)
    if not user_name:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(request, "queue.html", {
        "request": request,
        "user_name": user_name,
        "is_admin": is_admin
    })


@router.get("/queue/bar", response_class=HTMLResponse)
def queue_bar_partial(request: Request, db: Session = Depends(get_db)):
    user_name, _ = get_session_user(request)
    queue_items = crud.get_queue(db)
    queue_bar = []
    for item in queue_items:
        queue_bar.append({
            'id': item.id,
            'video_title': item.video.title if item.video else '',
            'user_name': item.user.name if item.user else '',
            'is_own_song': item.user.name == user_name if item.user else False
        })
    return templates.TemplateResponse(
        request, "queue_bar.html", {"queue": queue_bar, "request": request}
    )


@router.post("/queue/add", response_class=HTMLResponse)
def queue_add(
    request: Request,
    youtube_id: str = Form(...),
    title: str = Form(...),
    url: str = Form(...),
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None
):
    user_name, is_admin = get_session_user(request)
    if not user_name:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    video = crud.get_video_by_youtube_id(db, youtube_id)
    error = None
    file_path = None
    download_required = False
    if not video:
        download_required = True
    else:
        file_path = os.path.join('data', f'{youtube_id}.mp4')
        if not os.path.exists(file_path):
            download_required = True
    if download_required:
        try:
            file_path = download_youtube_video(youtube_id)
            if not video:
                video = crud.create_video(db, youtube_id=youtube_id, title=title, url=url)
        except Exception as e:
            error = f"Download failed: {str(e)}"
            queue_bar_html = queue_bar_partial(request, db).body.decode('utf-8')
            return HTMLResponse(queue_bar_html)
    if video and not error:
        user = crud.get_user_by_name(db, user_name)
        crud.add_to_queue(db, user_id=user.id, video_id=video.id)
    if background_tasks is not None:
        background_tasks.add_task(notify_queue_update)
    else:
        import asyncio
        asyncio.create_task(notify_queue_update())
    queue_items = crud.get_queue(db)
    queue_bar = []
    for item in queue_items:
        queue_bar.append({
            'id': item.id,
            'video_title': item.video.title if item.video else '',
            'user_name': item.user.name if item.user else '',
            'is_own_song': item.user.name == user_name if item.user else False
        })
    queue_bar_html = templates.TemplateResponse(
        request,
        "queue_bar.html",
        {"queue": queue_bar, "request": request}
    ).body.decode('utf-8')
    return HTMLResponse(queue_bar_html)


@router.post("/queue/delete/{queue_id}", response_class=HTMLResponse)
def queue_delete(
    request: Request,
    queue_id: int,
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None
):
    user_name, is_admin = get_session_user(request)
    if not user_name:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    queue_item = db.query(Queue).filter(Queue.id == queue_id).first()
    if not queue_item:
        queue_bar_html = queue_bar_partial(request, db).body.decode('utf-8')
        return HTMLResponse(queue_bar_html)
    if queue_item.user.name != user_name and not is_admin:
        queue_bar_html = queue_bar_partial(request, db).body.decode('utf-8')
        return HTMLResponse(queue_bar_html)
    crud.remove_from_queue(db, queue_id)
    if background_tasks is not None:
        background_tasks.add_task(notify_queue_update)
    else:
        import asyncio
        asyncio.create_task(notify_queue_update())
    queue_bar_html = queue_bar_partial(request, db).body.decode('utf-8')
    return HTMLResponse(queue_bar_html)


@router.get("/queue/stream")
async def queue_stream():
    """SSE endpoint for real-time queue updates."""
    return StreamingResponse(queue_update_event_generator(), media_type="text/event-stream") 