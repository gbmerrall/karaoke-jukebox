"""
Queue management service with SSE (Server-Sent Events) broadcasting.
Handles CRUD operations for the queue and real-time updates to clients.
"""

import asyncio
import json
from datetime import datetime
from typing import List, Dict, Optional, AsyncGenerator
from app.database import get_db
from app.config import settings
from fastapi.templating import Jinja2Templates
import logging

logger = logging.getLogger(__name__)

# Initialize templates
templates = Jinja2Templates(directory="app/templates")


class QueueManager:
    """Manages the video queue and broadcasts updates via SSE."""

    def __init__(self):
        """Initialize the queue manager."""
        # List of active SSE connections with user context
        # Each item: {"queue": asyncio.Queue, "username": str, "is_admin": bool}
        self._connections: List[Dict] = []

    async def add_to_queue(
        self,
        video_id: str,
        title: str,
        thumbnail_url: str,
        duration: int,
        views: int,
        username: str
    ) -> Dict:
        """
        Add a video to the queue.

        Args:
            video_id: YouTube video ID
            title: Video title
            thumbnail_url: URL to thumbnail
            duration: Duration in seconds
            views: View count
            username: User who queued the video

        Returns:
            Dict with queue item data

        Raises:
            ValueError: If video already in queue or queue is full
        """
        # Check if queue size limit is set and enforced
        if settings.max_queue_size > 0:
            current_size = await self.get_queue_size()
            if current_size >= settings.max_queue_size:
                raise ValueError(f"Queue is full (max: {settings.max_queue_size})")

        async with get_db() as db:
            # Check if THIS USER already has this video in queue
            # Multiple users can queue the same video (they each want to sing it)
            # Users can also re-queue a video after it's been played and removed
            cursor = await db.execute(
                "SELECT id FROM queue WHERE video_id = ? AND username = ? AND status != 'completed'",
                (video_id, username)
            )
            existing = await cursor.fetchone()

            if existing:
                raise ValueError("You have already queued this video")

            # Add to queue
            added_at = datetime.utcnow().isoformat()
            cursor = await db.execute(
                """
                INSERT INTO queue (video_id, title, thumbnail_url, duration, views, username, added_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'queued')
                """,
                (video_id, title, thumbnail_url, duration, views, username, added_at)
            )
            await db.commit()

            queue_id = cursor.lastrowid
            logger.info(f"Added to queue: {title} (ID: {queue_id}) by {username}")

            # Broadcast update to all connected clients
            await self.broadcast_queue_update()

            return {
                "id": queue_id,
                "video_id": video_id,
                "title": title,
                "thumbnail_url": thumbnail_url,
                "duration": duration,
                "views": views,
                "username": username,
                "added_at": added_at,
                "status": "queued"
            }

    async def remove_from_queue(self, queue_id: int, username: str = None, is_admin: bool = False) -> bool:
        """
        Remove a video from the queue.

        Args:
            queue_id: Queue item ID
            username: Username attempting removal (for ownership check)
            is_admin: Whether the user is admin (can remove any item)

        Returns:
            True if removed, False if not found or unauthorized

        Raises:
            PermissionError: If user doesn't own the item and is not admin
        """
        async with get_db() as db:
            # Check ownership if not admin
            if not is_admin and username:
                cursor = await db.execute(
                    "SELECT username FROM queue WHERE id = ?",
                    (queue_id,)
                )
                row = await cursor.fetchone()

                if not row:
                    return False

                if row["username"] != username:
                    raise PermissionError("You can only remove your own queued songs")

            # Remove from queue
            cursor = await db.execute(
                "DELETE FROM queue WHERE id = ?",
                (queue_id,)
            )
            await db.commit()

            if cursor.rowcount > 0:
                logger.info(f"Removed from queue: ID {queue_id} by {username or 'admin'}")
                await self.broadcast_queue_update()
                return True

            return False

    async def get_queue(self) -> List[Dict]:
        """
        Get all items in the queue, ordered by added_at.

        Returns:
            List of queue item dictionaries
        """
        async with get_db() as db:
            cursor = await db.execute(
                """
                SELECT id, video_id, title, thumbnail_url, duration, views, username, added_at, status
                FROM queue
                WHERE status != 'completed'
                ORDER BY added_at ASC
                """
            )
            rows = await cursor.fetchall()

            return [dict(row) for row in rows]

    async def get_queue_size(self) -> int:
        """Get the current number of items in the queue."""
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) as count FROM queue WHERE status != 'completed'"
            )
            row = await cursor.fetchone()
            return row["count"] if row else 0

    async def get_currently_playing(self) -> Optional[Dict]:
        """Get the currently playing queue item, if any."""
        async with get_db() as db:
            cursor = await db.execute(
                """
                SELECT id, video_id, title, thumbnail_url, duration, views, username, added_at, status
                FROM queue
                WHERE status = 'playing'
                LIMIT 1
                """
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def update_status(self, queue_id: int, status: str) -> bool:
        """
        Update the status of a queue item.

        Args:
            queue_id: Queue item ID
            status: New status ('queued', 'playing', 'completed')

        Returns:
            True if updated, False if not found
        """
        async with get_db() as db:
            cursor = await db.execute(
                "UPDATE queue SET status = ? WHERE id = ?",
                (status, queue_id)
            )
            await db.commit()

            if cursor.rowcount > 0:
                logger.info(f"Updated queue item {queue_id} status to: {status}")
                await self.broadcast_queue_update()
                return True

            return False

    async def cleanup_old_items(self, hours_threshold: int) -> int:
        """
        Remove queue items older than the specified threshold.

        Args:
            hours_threshold: Remove items older than this many hours

        Returns:
            Number of items removed
        """
        async with get_db() as db:
            cursor = await db.execute(
                """
                DELETE FROM queue
                WHERE datetime(added_at) < datetime('now', '-' || ? || ' hours')
                """,
                (hours_threshold,)
            )
            await db.commit()
            count = cursor.rowcount

            if count > 0:
                logger.info(f"Cleaned up {count} old queue items (older than {hours_threshold} hours)")
                await self.broadcast_queue_update()

            return count

    # SSE Broadcasting

    async def subscribe(self, username: str = None, is_admin: bool = False) -> AsyncGenerator[str, None]:
        """
        Subscribe to queue updates via SSE.

        Args:
            username: Username of the connected client
            is_admin: Whether the client is an admin

        Yields:
            SSE-formatted event strings

        Usage:
            async for event in queue_manager.subscribe(username, is_admin):
                # Send event to client
        """
        # Create a queue for this connection
        conn_queue = asyncio.Queue()
        # Store connection with user context
        conn_data = {"queue": conn_queue, "username": username, "is_admin": is_admin}
        self._connections.append(conn_data)
        logger.info(f"SSE client connected ({username}). Total connections: {len(self._connections)}")

        try:
            # Send initial queue state (rendered as HTML)
            initial_queue = await self.get_queue()
            html = self._render_queue_html(initial_queue, username, is_admin)
            yield self._format_sse_event("queue-update", html, is_html=True)

            # Send heartbeat and updates
            while True:
                try:
                    # Wait for update or timeout for heartbeat
                    event = await asyncio.wait_for(conn_queue.get(), timeout=30.0)
                    yield event
                except asyncio.TimeoutError:
                    # Send heartbeat to keep connection alive
                    yield self._format_sse_event("heartbeat", {"status": "ok"})

        except asyncio.CancelledError:
            logger.info("SSE client connection cancelled")
        finally:
            self._connections.remove(conn_data)
            logger.info(f"SSE client disconnected ({username}). Total connections: {len(self._connections)}")

    async def broadcast_queue_update(self) -> None:
        """Broadcast the current queue state to all connected SSE clients."""
        if not self._connections:
            return

        queue_data = await self.get_queue()

        # Send individualized HTML to each client
        dead_connections = []
        for conn_data in self._connections:
            try:
                # Render HTML for this specific client
                html = self._render_queue_html(
                    queue_data,
                    conn_data["username"],
                    conn_data["is_admin"]
                )
                event = self._format_sse_event("queue-update", html, is_html=True)
                conn_data["queue"].put_nowait(event)
            except Exception as e:
                logger.warning(f"Failed to send to SSE client: {e}")
                dead_connections.append(conn_data)

        # Clean up dead connections
        for conn in dead_connections:
            try:
                self._connections.remove(conn)
            except ValueError:
                pass

    def _render_queue_html(self, queue: List[Dict], username: str = None, is_admin: bool = False) -> str:
        """
        Render the queue HTML template.

        Args:
            queue: List of queue items
            username: Username of the client
            is_admin: Whether the client is an admin

        Returns:
            Rendered HTML string
        """
        # Create a fake request object for template rendering
        from starlette.datastructures import Headers
        fake_request = type('obj', (object,), {
            'headers': Headers({}),
            'url': type('obj', (object,), {'path': '/'})()
        })()

        # Use admin template for admin users, regular template for others
        template_name = "partials/admin_queue.html" if is_admin else "partials/queue.html"

        html = templates.get_template(template_name).render({
            "request": fake_request,
            "queue": queue,
            "username": username,
            "is_admin": is_admin
        })

        logger.debug(f"Rendered queue HTML for {username} (admin={is_admin}): {len(html)} chars, {len(queue)} items")
        return html

    def _format_sse_event(self, event_type: str, data: any, is_html: bool = False) -> str:
        """
        Format data as an SSE event.

        Args:
            event_type: Event type (e.g., 'queue-update', 'heartbeat')
            data: Data to send (HTML string or will be JSON-encoded)
            is_html: If True, data is HTML and won't be JSON-encoded

        Returns:
            SSE-formatted string
        """
        if is_html:
            # For multiline HTML, each line must be prefixed with "data: "
            lines = data.split('\n')
            data_lines = '\n'.join(f'data: {line}' for line in lines)
            return f"event: {event_type}\n{data_lines}\n\n"
        else:
            # For other data, JSON-encode
            json_data = json.dumps(data)
            return f"event: {event_type}\ndata: {json_data}\n\n"


# Global instance
queue_manager = QueueManager()
