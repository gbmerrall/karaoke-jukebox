"""Tests for the legacy UNIQUE(video_id) migration."""

import sqlite3

OLD_SCHEMA = """
CREATE TABLE queue (
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


async def test_migration_drops_unique_and_preserves_rows(tmp_path, monkeypatch):
    """init_db rebuilds a legacy table so duplicate video_ids are allowed."""
    from app.config import settings
    from app.database import init_db

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    db_path = tmp_path / "karaoke.db"

    # Seed a database created with the old UNIQUE(video_id) schema.
    conn = sqlite3.connect(db_path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO queue (video_id, title, username, added_at) "
        "VALUES ('dQw4w9WgXcQ', 'Song', 'alice', '2020-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()

    await init_db()

    conn = sqlite3.connect(db_path)
    try:
        # The constraint is gone from the rebuilt table.
        schema_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='queue'"
        ).fetchone()[0]
        assert "UNIQUE" not in schema_sql.upper()

        # The original row survived the rebuild.
        names = conn.execute(
            "SELECT username FROM queue WHERE video_id='dQw4w9WgXcQ'"
        ).fetchall()
        assert [r[0] for r in names] == ["alice"]

        # A second user can now queue the same video without an IntegrityError.
        conn.execute(
            "INSERT INTO queue (video_id, title, username, added_at) "
            "VALUES ('dQw4w9WgXcQ', 'Song', 'bob', '2020-01-01T00:01:00')"
        )
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM queue WHERE video_id='dQw4w9WgXcQ'"
        ).fetchone()[0]
        assert count == 2
    finally:
        conn.close()


async def test_migration_is_noop_on_clean_schema(tmp_path, monkeypatch):
    """Running init_db twice does not error and keeps the clean schema."""
    from app.config import settings
    from app.database import init_db

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    await init_db()
    await init_db()  # second run must be a no-op

    conn = sqlite3.connect(tmp_path / "karaoke.db")
    try:
        schema_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='queue'"
        ).fetchone()[0]
        assert "UNIQUE" not in schema_sql.upper()
    finally:
        conn.close()
