"""
crud.py

Contains CRUD operations for the karaoke app database.
"""
from sqlalchemy.orm import Session
from src import models
from sqlalchemy.exc import IntegrityError
from typing import Optional, List
import hashlib
import structlog

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer()
    ]
)
logger = structlog.get_logger()

def hash_password(password: str) -> str:
    """Hash a password using SHA256."""
    logger.info("hash_password_called")
    return hashlib.sha256(password.encode('utf-8')).hexdigest()

# --- User CRUD ---
def create_user(db: Session, name: str, is_admin: bool = False) -> Optional[models.User]:
    """Create a new user with the given name and admin status."""
    logger.info("create_user_called", name=name, is_admin=is_admin)
    name = name.strip()
    if not name:
        logger.warning("create_user_empty_name")
        return None
    user = models.User(name=name, is_admin=is_admin)
    db.add(user)
    try:
        db.commit()
        db.refresh(user)
        logger.info("create_user_success", user_id=user.id)
        return user
    except IntegrityError as e:
        db.rollback()
        logger.error("create_user_integrity_error", error=str(e))
        return None

def get_user(db: Session, user_id: int) -> Optional[models.User]:
    """Retrieve a user by ID."""
    logger.info("get_user_called", user_id=user_id)
    return db.query(models.User).filter(models.User.id == user_id).first()

def get_user_by_name(db: Session, name: str) -> Optional[models.User]:
    """Retrieve a user by name."""
    logger.info("get_user_by_name_called", name=name)
    return db.query(models.User).filter(models.User.name == name.strip()).first()

# --- Video CRUD ---
def create_video(db: Session, youtube_id: str, title: str, url: str) -> Optional[models.Video]:
    """Create a new video entry."""
    logger.info("create_video_called", youtube_id=youtube_id, title=title)
    youtube_id = youtube_id.strip()
    title = title.strip()
    url = url.strip()
    if not youtube_id or not title or not url:
        logger.warning("create_video_missing_fields")
        return None
    video = models.Video(youtube_id=youtube_id, title=title, url=url)
    db.add(video)
    try:
        db.commit()
        db.refresh(video)
        logger.info("create_video_success", video_id=video.id)
        return video
    except IntegrityError as e:
        db.rollback()
        logger.error("create_video_integrity_error", error=str(e))
        return None

def get_video(db: Session, video_id: int) -> Optional[models.Video]:
    """Retrieve a video by DB ID."""
    logger.info("get_video_called", video_id=video_id)
    return db.query(models.Video).filter(models.Video.id == video_id).first()

def get_video_by_youtube_id(db: Session, youtube_id: str) -> Optional[models.Video]:
    """Retrieve a video by YouTube ID."""
    logger.info("get_video_by_youtube_id_called", youtube_id=youtube_id)
    return db.query(models.Video).filter(models.Video.youtube_id == youtube_id.strip()).first()

# --- Queue CRUD ---
def add_to_queue(db: Session, user_id: int, video_id: int) -> Optional[models.Queue]:
    """Add a video to the queue for a user."""
    logger.info("add_to_queue_called", user_id=user_id, video_id=video_id)
    queue_item = models.Queue(user_id=user_id, video_id=video_id)
    db.add(queue_item)
    try:
        db.commit()
        db.refresh(queue_item)
        logger.info("add_to_queue_success", queue_id=queue_item.id)
        return queue_item
    except IntegrityError as e:
        db.rollback()
        logger.error("add_to_queue_integrity_error", error=str(e))
        return None

def get_queue(db: Session, limit: int = 50) -> List[models.Queue]:
    """Get the current queue, ordered FIFO."""
    logger.info("get_queue_called", limit=limit)
    return db.query(models.Queue).order_by(models.Queue.added_at.asc()).limit(limit).all()

def remove_from_queue(db: Session, queue_id: int) -> bool:
    """Remove a queue item by its ID."""
    logger.info("remove_from_queue_called", queue_id=queue_id)
    item = db.query(models.Queue).filter(models.Queue.id == queue_id).first()
    if item:
        db.delete(item)
        db.commit()
        logger.info("remove_from_queue_success", queue_id=queue_id)
        return True
    logger.warning("remove_from_queue_not_found", queue_id=queue_id)
    return False

def clear_queue(db: Session) -> None:
    """Remove all items from the queue."""
    logger.info("clear_queue_called")
    db.query(models.Queue).delete()
    db.commit()

# --- AdminConfig CRUD ---
def set_admin_config(db: Session, username: str, password: str) -> Optional[models.AdminConfig]:
    """Set or update the admin username and password hash."""
    logger.info("set_admin_config_called", username=username)
    username = username.strip()
    if not username or not password:
        logger.warning("set_admin_config_missing_fields")
        return None
    password_hash = hash_password(password)
    admin = db.query(models.AdminConfig).first()
    if admin:
        admin.username = username
        admin.password_hash = password_hash
    else:
        admin = models.AdminConfig(username=username, password_hash=password_hash)
        db.add(admin)
    db.commit()
    db.refresh(admin)
    logger.info("set_admin_config_success", username=username)
    return admin

def get_admin_config(db: Session) -> Optional[models.AdminConfig]:
    """Retrieve the admin config (username and password hash)."""
    logger.info("get_admin_config_called")
    return db.query(models.AdminConfig).first()

def verify_admin_password(db: Session, username: str, password: str) -> bool:
    """Verify the admin password for a given username."""
    logger.info("verify_admin_password_called", username=username)
    admin = db.query(models.AdminConfig).filter(models.AdminConfig.username == username.strip()).first()
    if not admin:
        logger.warning("verify_admin_password_not_found", username=username)
        return False
    result = admin.password_hash == hash_password(password)
    logger.info("verify_admin_password_result", username=username, result=result)
    return result 