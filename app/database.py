"""
Database layer using aiosqlite for async SQLite operations.
Simple, direct SQL queries without ORM overhead.
"""

import aiosqlite
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from app.config import settings


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
    Should be called on application startup.
    """
    db_path = settings.get_db_path()

    # Ensure data directory exists
    db_path.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(db_path) as db:
        await db.execute(CREATE_QUEUE_TABLE)
        await db.execute(CREATE_QUEUE_INDEX)
        await db.commit()


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
