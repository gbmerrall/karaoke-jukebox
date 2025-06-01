import asyncio
from fastapi import Request
from fastapi.templating import Jinja2Templates
from src import db
import secrets

# Jinja2 templates
templates = Jinja2Templates(directory="src/templates")

# Session helpers
SESSION_COOKIE = "karaoke_session"
COOKIE_MAX_AGE = 60 * 60 * 8  # 8 hours
COOKIE_SECRET = secrets.token_urlsafe(32)

# DB dependency

def get_db():
    db_session = db.SessionLocal()
    try:
        yield db_session
    finally:
        db_session.close()

# Session user helper
def get_session_user(request: Request):
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None, False
    parts = cookie.split("|", 1)
    if len(parts) != 2:
        return None, False
    user_name = parts[0]
    is_admin = parts[1] == "1"
    return user_name, is_admin

# SSE pub/sub
queue_update_subscribers = set()

async def queue_update_event_generator():
    """SSE generator for queue updates. Adds/removes subscriber queues and logs connections."""
    queue = asyncio.Queue()
    queue_update_subscribers.add(queue)
    print(f"[SSE] New subscriber connected. Total: {len(queue_update_subscribers)}")
    try:
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=15)
                yield f"data: {data}\n\n"
            except asyncio.TimeoutError:
                yield ": ping\n\n"
    finally:
        queue_update_subscribers.discard(queue)
        print(f"[SSE] Subscriber disconnected. Total: {len(queue_update_subscribers)}")

async def notify_queue_update():
    dead_queues = []
    for queue in list(queue_update_subscribers):
        try:
            queue.put_nowait("update")
        except Exception:
            dead_queues.append(queue)
    for dq in dead_queues:
        queue_update_subscribers.discard(dq) 