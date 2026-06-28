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


# SQL schema for the queue table.
# Intentionally NO unique constraint on video_id: multiple users can each queue
# the same song (they each want to sing it), and a user can re-queue a song
# after it has played. Duplicate prevention is per (video_id, username) in the
# application layer (see queue_manager.add_to_queue).
QUEUE_COLUMNS = (
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "video_id TEXT NOT NULL, "
    "title TEXT NOT NULL, "
    "thumbnail_url TEXT, "
    "duration INTEGER, "
    "views INTEGER, "
    "username TEXT NOT NULL, "
    "added_at TEXT NOT NULL, "
    "status TEXT NOT NULL DEFAULT 'queued' "
    "CHECK(status IN ('queued', 'playing', 'completed'))"
)

CREATE_QUEUE_TABLE = f"CREATE TABLE IF NOT EXISTS queue ({QUEUE_COLUMNS})"

# Create index for faster queries
CREATE_QUEUE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_queue_added_at ON queue(added_at)
"""


async def _migrate_drop_unique_video_id(db: aiosqlite.Connection) -> None:
    """Rebuild the queue table to drop a legacy UNIQUE(video_id) constraint.

    Older databases were created with UNIQUE(video_id), which breaks the core
    feature of multiple users queueing the same song. SQLite cannot DROP a
    constraint in place, so we detect it in the stored table SQL and rebuild
    the table (rename -> create clean -> copy -> drop). No-op if the constraint
    is absent.

    Args:
        db: An open aiosqlite connection.
    """
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='queue'"
    )
    row = await cursor.fetchone()
    if not row or not row[0] or "UNIQUE" not in row[0].upper():
        return

    logger.info("Migrating queue table: dropping legacy UNIQUE(video_id) constraint")
    columns = "id, video_id, title, thumbnail_url, duration, views, username, added_at, status"
    await db.execute("ALTER TABLE queue RENAME TO queue_legacy")
    await db.execute(f"CREATE TABLE queue ({QUEUE_COLUMNS})")
    await db.execute(
        f"INSERT INTO queue ({columns}) SELECT {columns} FROM queue_legacy"
    )
    await db.execute("DROP TABLE queue_legacy")
    await db.commit()
    logger.info("Queue table migration complete")


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

            # Migrate away from any legacy UNIQUE(video_id) constraint.
            await _migrate_drop_unique_video_id(db)

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

            logger.debug(
                f"Database read test successful (current queue items: {count[0]})"
            )

            # Verify we can write to the database (test with a transaction)
            await db.execute("BEGIN")
            await db.execute("ROLLBACK")

            logger.debug("Database write test successful")

    except aiosqlite.Error as e:
        raise RuntimeError(f"Database initialization failed: {e}") from e
    except Exception as e:
        raise RuntimeError(
            f"Unexpected error during database initialization: {e}"
        ) from e

    logger.info("Database initialization complete and verified")


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

    # WAL lets a reader and a writer coexist; busy_timeout makes a contended
    # writer wait up to 5s instead of failing immediately with "database is
    # locked". Both pragmas are per-connection. foreign_keys for integrity,
    # row_factory for dict-like access.
    await db.execute("PRAGMA journal_mode = WAL")
    await db.execute("PRAGMA busy_timeout = 5000")
    await db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = aiosqlite.Row

    try:
        yield db
    finally:
        await db.close()
