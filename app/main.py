"""
Karaoke Jukebox - Main FastAPI application.

A mobile-first karaoke jukebox web app that allows users to search for
karaoke videos, queue them, and play through Chromecast with real-time
queue updates via Server-Sent Events.
"""

import asyncio
import logging
import sys
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings
from app.database import init_db
from app.services.queue_manager import queue_manager
from app.routes import auth, search, queue, admin

# Configure logging (will be reconfigured with LOG_LEVEL setting during startup)
logging.basicConfig(
    level=logging.INFO,  # Default level before settings are fully loaded
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)


# Scheduler for cleanup jobs
scheduler = AsyncIOScheduler()


async def cleanup_old_queue_items():
    """
    Scheduled job to clean up old queue items.
    Runs at configured intervals.
    """
    try:
        threshold = settings.queue_cleanup_threshold_hours
        count = await queue_manager.cleanup_old_items(threshold)
        if count > 0:
            logger.info(f"Cleanup job removed {count} old queue items")
    except Exception as e:
        logger.error(f"Error in cleanup job: {e}", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context manager for startup and shutdown events.
    """
    # Startup
    logger.info("Starting Karaoke Jukebox application...")

    # Configure logging level from settings
    log_level = getattr(logging, settings.log_level)
    logging.getLogger().setLevel(log_level)
    logger.info(f"Log level set to: {settings.log_level}")

    # Initialize database
    logger.info("Initializing database...")
    await init_db()
    logger.info("Database initialized")

    # Reset orphaned queue items (items stuck in 'playing' status from previous run)
    reset_count = await queue_manager.reset_orphaned_items()
    if reset_count > 0:
        logger.info(f"Reset {reset_count} orphaned queue item(s)")

    # Set event loop reference for chromecast service (for sync/async bridge)
    from app.services.chromecast import chromecast_service
    chromecast_service.set_event_loop(asyncio.get_running_loop())

    # Ensure data directories exist
    settings.get_videos_dir().mkdir(parents=True, exist_ok=True)
    logger.info(f"Data directory: {settings.data_dir}")

    # Check for ffmpeg (required for video downloads)
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        logger.info(f"ffmpeg found: {ffmpeg_path}")
    else:
        logger.warning("⚠️  ffmpeg NOT FOUND! Video downloads will fail.")
        logger.warning("   Install ffmpeg: https://ffmpeg.org/download.html")
        logger.warning("   macOS: brew install ffmpeg")
        logger.warning("   Ubuntu: apt-get install ffmpeg")

    # Check and log server configuration for Chromecast
    logger.info("=" * 60)
    logger.info("SERVER CONFIGURATION FOR CHROMECAST")
    logger.info("=" * 60)

    if settings.is_docker():
        logger.info("Environment: Docker container detected")
        if settings.server_host:
            logger.info(f"Server Host: {settings.server_host} (from SERVER_HOST env var)")
        else:
            logger.error("❌ SERVER_HOST is NOT SET!")
            logger.error("   Chromecast will NOT be able to access videos!")
            logger.error("   Set SERVER_HOST to your Docker host's IP address")
            logger.error("   Example: SERVER_HOST=192.168.1.100")
    else:
        logger.info("Environment: Development (not Docker)")
        if settings.server_host:
            logger.info(f"Server Host: {settings.server_host} (from SERVER_HOST env var)")
        else:
            detected_host = settings.get_local_ip()
            logger.info(f"Server Host: {detected_host} (auto-detected)")

    logger.info(f"Server Port: {settings.server_port}")

    # Show example Chromecast URL
    example_url = settings.get_video_url("EXAMPLE_VIDEO_ID")
    logger.info(f"Example Chromecast URL: {example_url}")
    logger.info("Chromecasts must be able to reach this URL on your network")
    logger.info("=" * 60)

    # Start cleanup scheduler
    if settings.queue_cleanup_interval_hours > 0:
        scheduler.add_job(
            cleanup_old_queue_items,
            "interval",
            hours=settings.queue_cleanup_interval_hours,
            id="cleanup_queue"
        )
        scheduler.start()
        logger.info(
            f"Cleanup scheduler started "
            f"(every {settings.queue_cleanup_interval_hours} hours, "
            f"threshold: {settings.queue_cleanup_threshold_hours} hours)"
        )

    logger.info("Application startup complete")

    yield

    # Shutdown
    logger.info("Shutting down application...")

    # Stop scheduler
    if scheduler.running:
        scheduler.shutdown()
        logger.info("Cleanup scheduler stopped")

    logger.info("Application shutdown complete")


# Create FastAPI app
app = FastAPI(
    title="Karaoke Jukebox",
    description="Mobile-first karaoke jukebox with Chromecast support",
    version="2.0.0",
    lifespan=lifespan
)


# Register routes
app.include_router(auth.router)
app.include_router(search.router)
app.include_router(queue.router)
app.include_router(admin.router)


# Mount static files
# Videos directory (for Chromecast playback)
videos_path = settings.get_videos_dir()
if videos_path.exists():
    app.mount(
        "/data/videos",
        StaticFiles(directory=str(videos_path)),
        name="videos"
    )
else:
    logger.warning(f"Videos directory does not exist: {videos_path}")

# Static assets (CSS, JS)
static_path = Path("app/static")
if static_path.exists():
    app.mount(
        "/static",
        StaticFiles(directory=str(static_path)),
        name="static"
    )


# Root redirect (for convenience)
@app.get("/", include_in_schema=False)
async def root():
    """Redirect root to login page (handled by auth.router)."""
    # This is just documentation - the actual route is in auth.router
    pass  # auth.router handles "/"


# Health check endpoint
@app.get("/health")
async def health_check():
    """Health check endpoint for monitoring."""
    return {
        "status": "healthy",
        "queue_size": await queue_manager.get_queue_size(),
        "is_playing": False  # Could check chromecast_service.is_playing
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.server_port,
        reload=True,
        log_level="info"
    )
