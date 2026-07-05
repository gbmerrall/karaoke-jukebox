# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Karaoke Jukebox - A mobile-first web application for group karaoke parties. Users search YouTube for karaoke videos, queue songs, and the app plays them through Chromecast with real-time queue updates.

The application code lives at the repository root under `app/`. Dependencies are
managed with **uv** (declared in `pyproject.toml`, pinned in the committed
`uv.lock`; Python 3.13 is pinned via `.python-version`). The ASGI app object is
`app.main:app`.

## Development Commands

### Running the Application

From the repository root:

```bash
# Start development server (preferred method)
make run
# or
./run.sh
# or directly
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The app runs at http://localhost:8000

### Environment Setup

```bash
# Install dependencies (runtime + dev group, installed by default) from the repository root
uv sync

# Create and populate .env (generates SECRET_KEY)
./setup.sh
```

### Git Worktrees

Each worktree has its own `.venv` (not shared with the main checkout). Run `uv sync`
inside a new worktree before relying on any diagnostics or type checking there.

This repo uses `ty` as the authoritative type checker (see Python Coding Guidelines).
Claude Code's `pyright-lsp` plugin also runs in the background for inline diagnostics,
but there is no `pyrightconfig.json`/`[tool.pyright]` in this repo, so Pyright falls
back to auto-detecting a `.venv` folder. In a freshly created worktree that hasn't been
`uv sync`'d yet, that `.venv` exists but is empty, which makes Pyright report every
third-party import (fastapi, pychromecast, etc.) as unresolved. Treat those
import-resolution errors as noise from an unsynced worktree, not real bugs — don't
chase them by editing Pyright config; run `uv sync` and/or defer to `ty` instead.

**Required environment variables** (in `.env` at the repository root):
- `ADMIN_PASSWORD` - Admin login password
- `YOUTUBE_API_KEY` - YouTube Data API v3 key
- `SECRET_KEY` - Session signing key (generate with: `python -c "import secrets; print(secrets.token_hex(32))"`)
- `SERVER_HOST` - Required for Docker, auto-detected in dev mode
- `SERVER_PORT` - Default: 8000
- `LOG_LEVEL` - Default: INFO

### Make targets

```bash
make test        # fast unit tests (skips the network canary)
make canary      # opt-in yt-dlp integration test (downloads a live clip)
make preflight   # bump yt-dlp + run the canary (the deploy gate)
make build       # preflight, then docker build (build blocked if canary fails)
make lint        # ruff check + ruff format --check
make run         # local dev server
```

### Testing

```bash
# Fast suite (default - no network, no secrets needed)
uv run pytest

# yt-dlp canary (opt-in, hits live YouTube)
uv run pytest -m integration --run-integration
```

### Docker

```bash
# Build (from repository root; DOCKER_BUILDKIT=1 enables apt caching)
DOCKER_BUILDKIT=1 docker build -t karaoke-jukebox .

# Run (SERVER_HOST MUST be set to Docker host IP)
docker run -d \
  -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  -e ADMIN_PASSWORD=password \
  -e YOUTUBE_API_KEY=key \
  -e SECRET_KEY=key \
  -e SERVER_HOST=192.168.1.100 \
  karaoke-jukebox

# Or via docker-compose (reads .env; fails loudly if required vars are unset)
docker-compose up -d
```

### Code Quality

```bash
# Lint with ruff
uv run ruff check .

# Format with ruff
uv run ruff format .
```

## Architecture Overview

### Technology Stack

- **Backend**: FastAPI + Python 3.13 + aiosqlite
- **Frontend**: Jinja2 templates + HTMX + DaisyUI (Tailwind CSS)
- **Real-time**: Server-Sent Events (SSE) for queue updates
- **Media**: yt-dlp (requires ffmpeg) + pychromecast
- **Deployment**: Gunicorn + Uvicorn workers

### Key Architectural Patterns

#### 1. Hybrid Threading Model

**Critical**: The app uses both asyncio and threading:
- **AsyncIO**: FastAPI routes, database access, SSE connections
- **Threading**: Chromecast playback loop (pychromecast is synchronous)

**Sync-to-Async Bridge** (`app/services/playout.py`):
```python
def _playout_loop(self):  # Thread
    # Creates new event loop to call async database functions
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(async_function())
    loop.close()
```

#### 2. Server-Sent Events (SSE) Broadcasting

**Pattern**: Individualized HTML rendering for each connected client

- Each SSE connection stores: `{"queue": asyncio.Queue, "username": str, "is_admin": bool}`
- Broadcasting renders different HTML per client (delete buttons show based on ownership/admin status)
- Multiline HTML requires each line prefixed with `data:` for SSE spec compliance
- 30-second heartbeat prevents connection timeout

**Location**: `app/services/queue_manager.py:subscribe()` and `broadcast_queue_update()`

#### 3. Chromecast Playback State Machine

**Thread-safe state management**:
- `threading.Lock` for state access
- `threading.Event` for `skip_requested` and `stop_requested`
- Background daemon thread runs `_playout_loop()`

**Critical playback details** (learned from iterative testing):

```python
# Use BUFFERED stream type (NOT "LIVE") for video files
cast.play_media(url, "video/mp4", stream_type="BUFFERED")

# MUST wait for session before monitoring
cast.media_controller.session_active_event.wait(timeout=30)

# CRITICAL: Wait for fresh status after session activation
# The status object is STALE immediately after session_active_event.wait() returns
# It contains state from the previous video (e.g., IDLE/FINISHED)
# Chromecast sends first update within ~100ms, we wait 500ms to be safe
time.sleep(0.5)

# Check idle_reason to distinguish completion types
if mc_status.player_state == "IDLE":
    if idle_reason == "FINISHED":  # Success
    elif idle_reason == "ERROR":   # Failed
    elif idle_reason == "INTERRUPTED" or None:  # New media loading, keep waiting
```

**Location**: `app/services/players/chromecast_player.py:play()`

#### 4. Video URL Generation for Chromecast

**Challenge**: Chromecast needs HTTP URLs accessible on local network

- **Development**: Auto-detects local IP using socket trick (`config.py:get_local_ip()`)
- **Docker**: MUST explicitly set `SERVER_HOST` env var to Docker host IP (not container IP)
- **Detection**: Checks `/.dockerenv` to identify Docker environment

**If SERVER_HOST not set in Docker, Chromecast cannot reach videos**

#### 5. Database Schema

**Queue Table** (SQLite):
```sql
CREATE TABLE queue (
    id INTEGER PRIMARY KEY,
    video_id TEXT NOT NULL,
    title TEXT NOT NULL,
    thumbnail_url TEXT,
    duration INTEGER,
    views INTEGER,
    username TEXT NOT NULL,
    added_at TEXT NOT NULL,
    status TEXT DEFAULT 'queued' CHECK(status IN ('queued', 'playing', 'completed'))
)
```

**Important**: No unique constraint on `video_id` - multiple users can queue the same song (they each want to sing it). Duplicate check is `video_id + username` in application layer.

#### 6. Session Management

**Stateless cookie-based sessions** using `itsdangerous.URLSafeSerializer`:
- Signed cookies prevent tampering
- Stores: `{"username": str, "is_admin": bool}`
- HTTPOnly, SameSite=Lax, 24-hour expiry
- Single admin account (username: "admin")

## Critical Implementation Details

### Chromecast Connection Management

When scanning for devices (`app/services/players/chromecast_player.py:discover_devices()`):
1. **MUST disconnect existing Chromecast connection first** (lines 84-96)
2. Otherwise AsyncZeroconf conflicts with existing connection
3. Uses `AsyncZeroconf` to avoid blocking event loop

### SSE Event Formatting

For multiline HTML in SSE events:
```python
# Correct format
lines = html.split('\n')
data_lines = '\n'.join(f'data: {line}' for line in lines)
return f"event: queue-update\n{data_lines}\n\n"
```

### Queue Item Removal Logic

**From playout loop** (`app/services/playout.py:_playout_loop()`):
- Remove on `FINISHED` (completed successfully)
- Remove on skip request
- **Keep in queue** on `ERROR`, `UNKNOWN`, or stop request (allows retry)

### Download Service

Uses `yt-dlp` in thread pool to avoid blocking:
```python
await asyncio.to_thread(self._download_sync, video_id, ydl_opts)
```

**Format**: `bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best`

**Requires ffmpeg** - App checks on startup and logs warning if missing

### YouTube Search

Auto-appends "karaoke" to search queries. Uses YouTube Data API v3 with:
- Filter: `type="video"`, `videoCategoryId="10"` (Music)
- Order: `relevance` (YouTube's ranking)
- Duration parsed from ISO 8601 using `isodate` library

## Common Development Workflows

### Adding a New Route

1. Create route in appropriate file (`app/routes/`)
2. Include router in `app/main.py`
3. Use `require_session()` or `require_admin()` dependency for auth
4. For HTMX endpoints, return `TemplateResponse` with partial HTML
5. Trigger SSE updates via `queue_manager.broadcast_queue_update()` if queue changes

### Modifying Queue Behavior

1. Database operations: `app/services/queue_manager.py`
2. Playback logic: `app/services/playout.py:_playout_loop()`
3. UI rendering: `app/templates/partials/queue.html` (user) and `admin_queue.html` (admin)
4. Always broadcast updates after queue changes

### Testing Chromecast Changes

1. Update configuration in any chromecast test script (CAST_NAME, CAST_IP, SERVER_HOST)
2. Ensure videos exist in `data/videos/`
3. Run the test script and check logs for playback state transitions

## File Organization

```
.
├── app/
│   ├── main.py              # FastAPI app + lifespan (startup/shutdown)
│   ├── config.py            # Settings with Docker detection
│   ├── database.py          # SQLite async context manager
│   ├── routes/              # FastAPI route handlers
│   │   ├── auth.py          # Login/logout + session mgmt
│   │   ├── search.py        # YouTube search + queue addition
│   │   ├── queue.py         # SSE endpoint + queue operations
│   │   └── admin.py         # Chromecast control + admin queue mgmt
│   ├── services/            # Business logic
│   │   ├── youtube.py       # YouTube API search
│   │   ├── download.py      # yt-dlp video downloads
│   │   ├── playout.py       # Queue policy + playout thread (device-independent)
│   │   ├── players/         # Player contract + ChromecastPlayer backend
│   │   └── queue_manager.py # Queue CRUD + SSE broadcasting
│   └── templates/           # Jinja2 templates (HTMX-based)
├── data/
│   ├── videos/              # Downloaded MP4 files (served to Chromecast)
│   └── karaoke.db           # SQLite database
├── tests/                   # pytest suite (fast unit tests + opt-in yt-dlp canary)
├── run.sh                   # Development server launcher (uvicorn app.main:app)
├── setup.sh                 # Creates .env and generates SECRET_KEY
├── Makefile                 # Dev/deploy task runner (test, canary, preflight, build)
├── Dockerfile               # Production image (gunicorn + uvicorn workers)
└── docker-compose.yml       # Compose config (host networking for Chromecast)
```

## Troubleshooting Notes

**"Chromecast not found"**: Ensure existing connection is disconnected before scanning. Check firewall allows mDNS (port 5353).

**"Videos won't play on Chromecast"**: Verify `SERVER_HOST` is correct (especially in Docker). Test URL accessibility: `http://{SERVER_HOST}:{SERVER_PORT}/data/videos/{video_id}.mp4`

**"Queue not updating in browser"**: Check browser console for SSE connection errors. Verify `/queue/sse` endpoint is accessible. Safari has stricter SSE requirements.

**"ffmpeg not found"**: Install ffmpeg before running. macOS: `brew install ffmpeg`

**Session issues**: Session cookies are signed with `SECRET_KEY`. Changing the key invalidates all sessions.

## Dependencies

**Runtime** (declared in `pyproject.toml` under `[project].dependencies`, pinned in `uv.lock`):
- fastapi, uvicorn[standard], gunicorn
- pychromecast, zeroconf (Chromecast)
- yt-dlp (video downloads)
- google-api-python-client (YouTube API)
- aiosqlite (async database)
- jinja2, python-multipart, itsdangerous (web framework)
- apscheduler (cleanup jobs)
- pydantic-settings, isodate

**Dev** (declared in `pyproject.toml` under `[dependency-groups].dev`):
- ruff (linting/formatting)
- pytest, pytest-asyncio, pytest-cov (unit tests + opt-in yt-dlp canary)
- httpx2 (Starlette/FastAPI TestClient HTTP backend; tests only)

**Docker dependency manifest**: `requirements.txt` is a generated artifact and
is NOT committed (it is gitignored). The Dockerfile is multi-stage: an `export`
stage runs `uv export` to generate `requirements.txt` from the committed
`uv.lock`, and the runtime stage `pip install`s it (uv never enters the runtime
image). `uv.lock` is the single source of truth, so Dependabot's `uv` ecosystem
updates `uv.lock` directly. To bump deps: update `uv.lock` (`uv sync` or a
Dependabot PR) and rebuild. `make requirements` can still write a local
`requirements.txt` for inspection, but the build does not depend on it.

**System**:
- Python 3.13
- ffmpeg (required for video downloads)
- deno (required at runtime for yt-dlp's JS-challenge solver)
