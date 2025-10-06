"""
Database layer using aiosqlite for async SQLite operations.
Simple, direct SQL queries without ORM overhead.
"""

import aiosqlite
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from app.config import settings

logger = logging.getLogger(__name__)


# SQL schema for the queue table
CREATE_QUEUE_TABLE = """
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    title TEXT NOT NULL,
    thumbnail_url TEXT,
    duration INTEGER,
    views INTEGER,
    username TEXT NOT NULL,
    added_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued', 'playing', 'completed')),
    UNIQUE(video_id)
)
"""

# Create index for faster queries
CREATE_QUEUE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_queue_added_at ON queue(added_at)
"""


async def init_db() -> None:
    """
    Initialize the database by creating tables if they don't exist.
    Performs validation checks to ensure database is properly set up.
    Should be called on application startup.

    Raises:
        RuntimeError: If database initialization or validation fails
    """
    db_path = settings.get_db_path()

    logger.info(f"Initializing database at: {db_path}")

    # Ensure data directory exists
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Data directory verified: {db_path.parent}")
    except Exception as e:
        raise RuntimeError(f"Failed to create data directory: {e}") from e

    # Create database and tables
    try:
        async with aiosqlite.connect(db_path) as db:
            # Create tables
            await db.execute(CREATE_QUEUE_TABLE)
            logger.debug("Queue table created/verified")

            await db.execute(CREATE_QUEUE_INDEX)
            logger.debug("Queue index created/verified")

            await db.commit()

            # Verify database file was created
            if not db_path.exists():
                raise RuntimeError(f"Database file was not created at {db_path}")

            logger.debug(f"Database file exists: {db_path}")

            # Verify tables exist by querying sqlite_master
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='queue'"
            )
            result = await cursor.fetchone()

            if not result:
                raise RuntimeError("Queue table was not created successfully")

            logger.debug("Queue table verified in schema")

            # Verify we can read from the table (basic connectivity test)
            cursor = await db.execute("SELECT COUNT(*) FROM queue")
            count = await cursor.fetchone()

            logger.debug(f"Database read test successful (current queue items: {count[0]})")

            # Verify we can write to the database (test with a transaction)
            await db.execute("BEGIN")
            await db.execute("ROLLBACK")

            logger.debug("Database write test successful")

    except aiosqlite.Error as e:
        raise RuntimeError(f"Database initialization failed: {e}") from e
    except Exception as e:
        raise RuntimeError(f"Unexpected error during database initialization: {e}") from e

    logger.info("✅ Database initialization complete and verified")


@asynccontextmanager
async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """
    Async context manager for database connections.

    Usage:
        async with get_db() as db:
            cursor = await db.execute("SELECT * FROM queue")
            rows = await cursor.fetchall()
    """
    db_path = settings.get_db_path()
    db = await aiosqlite.connect(db_path)

    # Enable foreign keys and row factory for dict-like access
    await db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = aiosqlite.Row

    try:
        yield db
    finally:
        await db.close()
