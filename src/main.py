from fastapi import FastAPI, Request, Form, Depends, Response, Query, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette import status
from contextlib import asynccontextmanager
from src import crud, db
from sqlalchemy.orm import Session
import secrets
from src.youtube_utils import search_youtube
import os
import structlog
import asyncio
from src.models import Queue
from src.config import queue_settings

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime, timedelta
from src.admin_routes import router as admin_router
from src.queue_routes import router as queue_router
from src.utils import (
    get_session_user,
    notify_queue_update,
    templates,
    get_db,
)

# Configure structlog
structlog.configure(
    processors=[structlog.processors.TimeStamper(fmt="iso"), structlog.dev.ConsoleRenderer()]
)
logger = structlog.get_logger()

# Setup scheduler
scheduler = AsyncIOScheduler()

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

# --- Admin password enforcement ---
admin_password_missing = ADMIN_PASSWORD is None or ADMIN_PASSWORD == ""

def admin_password_blocker():
    return HTMLResponse(
        content="<h1 style='color:red;text-align:center;margin-top:20vh;'>Admin Password Not Set!</h1>",
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )

def get_db():
    db_session = db.SessionLocal()
    try:
        yield db_session
    finally:
        db_session.close()

def set_session_cookie(response: Response, user_name: str, is_admin: bool):
    """Set a secure session cookie with user_name and is_admin."""
    value = f"{user_name}|{int(is_admin)}"
    response.set_cookie(
        key=SESSION_COOKIE,
        value=value,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=False,  # Set to True in production with HTTPS
        samesite="lax",
    )

def get_session_user(request: Request):
    """Retrieve user_name and is_admin from the session cookie."""
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None, False
    parts = cookie.split("|", 1)
    if len(parts) != 2:
        return None, False
    user_name = parts[0]
    is_admin = parts[1] == "1"
    return user_name, is_admin

# Define scheduled cleanup function
async def cleanup_old_queue_items():
    """Remove queue items older than the configured threshold."""
    try:
        logger.info(
            "cleanup_old_queue_items_started",
            threshold_hours=queue_settings.CLEANUP_THRESHOLD_HOURS
        )
        db_session = db.SessionLocal()
        try:
            cutoff_time = datetime.utcnow() - timedelta(hours=queue_settings.CLEANUP_THRESHOLD_HOURS)
            old_items = db_session.query(Queue).filter(Queue.added_at < cutoff_time).all()
            count = len(old_items)
            if count > 0:
                for item in old_items:
                    db_session.delete(item)
                db_session.commit()
                logger.info(
                    "cleanup_old_queue_items_success",
                    count=count,
                    threshold_hours=queue_settings.CLEANUP_THRESHOLD_HOURS
                )
                # Notify queue update listeners
                asyncio.create_task(notify_queue_update())
            else:
                logger.info(
                    "cleanup_old_queue_items_none_found",
                    threshold_hours=queue_settings.CLEANUP_THRESHOLD_HOURS
                )
        finally:
            db_session.close()
    except Exception as e:
        logger.error(
            "cleanup_old_queue_items_error",
            error=str(e),
            threshold_hours=queue_settings.CLEANUP_THRESHOLD_HOURS
        )

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for FastAPI app startup and shutdown."""
    # Startup
    scheduler.add_job(
        cleanup_old_queue_items,
        IntervalTrigger(hours=queue_settings.CLEANUP_INTERVAL_HOURS),
        id="cleanup_old_queue_items",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "scheduler_started",
        cleanup_interval_hours=queue_settings.CLEANUP_INTERVAL_HOURS,
        cleanup_threshold_hours=queue_settings.CLEANUP_THRESHOLD_HOURS
    )
    
    yield
    
    # Shutdown
    scheduler.shutdown()
    logger.info("scheduler_stopped")

# Initialize FastAPI app
app = FastAPI(lifespan=lifespan)

# Add middleware if admin password is missing
if admin_password_missing:
    @app.middleware("http")
    async def block_all_requests(request, call_next):
        return admin_password_blocker()

# Mount static files
app.mount("/static", StaticFiles(directory="src/static"), name="static")
app.mount("/data", StaticFiles(directory="data"), name="data")

# Configure Jinja2 templates
templates = Jinja2Templates(directory="src/templates")

# Include API router
app.include_router(admin_router)
app.include_router(queue_router)

SESSION_COOKIE = "karaoke_session"
COOKIE_MAX_AGE = 60 * 60 * 8  # 8 hours
COOKIE_SECRET = secrets.token_urlsafe(32)

@app.get("/", response_class=HTMLResponse)
def read_root(request: Request):
    """Render the home page with the login form."""
    user_name, is_admin = get_session_user(request)
    if user_name:
        # Already logged in, redirect to appropriate page
        if is_admin:
            return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)
        else:
            return RedirectResponse(url="/search", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(request, "index.html", {"request": request, "error": None})

@app.post("/", response_class=HTMLResponse)
def index_login_post(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(None),
    db: Session = Depends(get_db),
):
    """Unified login for users and admin on the index page."""
    username = username.strip()
    if not username:
        return templates.TemplateResponse(
            request, "index.html", {"request": request, "error": "Name is required."}
        )
    if username.lower() == "admin":
        if not password:
            return templates.TemplateResponse(
                request, "index.html", {"request": request, "error": "Password required for admin."}
            )
        if not ADMIN_PASSWORD or password != ADMIN_PASSWORD:
            return templates.TemplateResponse(
                request, "index.html", {"request": request, "error": "Invalid admin credentials."}
            )
        response = RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)
        set_session_cookie(response, user_name="admin", is_admin=True)
        return response
    else:
        # Regular user: create user if not exists, set session/cookie
        user = crud.get_user_by_name(db, username)
        if not user:
            user = crud.create_user(db, name=username)
        response = RedirectResponse(url="/search", status_code=status.HTTP_302_FOUND)
        set_session_cookie(response, user_name=username, is_admin=False)
        return response

@app.get("/logout")
def logout(response: Response):
    """Clear the session cookie and redirect to index."""
    response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    response.delete_cookie(SESSION_COOKIE)
    return response

@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request, query: str = Query(None)):
    """Show the search page and handle YouTube search with 'karaoke' appended."""
    user_name, is_admin = get_session_user(request)
    logger.info("search_page_accessed", user_name=user_name, query=query)
    if not user_name:
        logger.warning("unauthorized_search_access", reason="not logged in")
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    results = []
    if query:
        try:
            results = search_youtube(query)
            logger.info("youtube_search_success", query=query, result_count=len(results))
        except Exception as e:
            logger.error("youtube_search_error", query=query, error=str(e))
            results = []
    context = {
        "request": request,
        "user_name": user_name,
        "is_admin": is_admin,
        "results": results,
        "query": query or "",
    }
    if request.headers.get("hx-request") == "true":
        logger.info("using template search_results.html")
        return templates.TemplateResponse(request, "search_results.html", context)
    else:
        logger.info("using template search.html")
        return templates.TemplateResponse(request, "search.html", context)

# --- User Endpoints ---
from pydantic import BaseModel, Field
from typing import Optional, Annotated
from fastapi import HTTPException, Depends, status
from sqlalchemy.orm import Session
from src import crud

class UserCreate(BaseModel):
    name: Annotated[str, Field(strip_whitespace=True, min_length=1, max_length=50)]
    is_admin: Optional[bool] = False

class UserOut(BaseModel):
    id: int
    name: str
    is_admin: bool
    class Config:
        from_attributes = True

@app.post("/api/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(user: UserCreate, db: Session = Depends(get_db)):
    """Create a new user."""
    db_user = crud.create_user(db, name=user.name, is_admin=user.is_admin)
    if not db_user:
        raise HTTPException(status_code=400, detail="User creation failed or name already exists.")
    return db_user

@app.get("/api/users/{user_id}", response_model=UserOut)
def get_user(user_id: int, db: Session = Depends(get_db)):
    """Get a user by ID."""
    user = crud.get_user(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return user

# --- Video Endpoints ---
class VideoCreate(BaseModel):
    youtube_id: Annotated[str, Field(strip_whitespace=True, min_length=1)]
    title: Annotated[str, Field(strip_whitespace=True, min_length=1)]
    url: Annotated[str, Field(strip_whitespace=True, min_length=1)]

class VideoOut(BaseModel):
    id: int
    youtube_id: str
    title: str
    url: str
    downloaded: bool
    class Config:
        from_attributes = True

@app.post("/api/videos", response_model=VideoOut, status_code=status.HTTP_201_CREATED)
def create_video(video: VideoCreate, db: Session = Depends(get_db)):
    """Create a new video entry."""
    db_video = crud.create_video(db, youtube_id=video.youtube_id, title=video.title, url=video.url)
    if not db_video:
        raise HTTPException(status_code=400, detail="Video creation failed or already exists.")
    return db_video

@app.get("/api/videos/{video_id}", response_model=VideoOut)
def get_video(video_id: int, db: Session = Depends(get_db)):
    """Get a video by DB ID."""
    video = crud.get_video(db, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found.")
    return video
