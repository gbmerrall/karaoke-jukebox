# Development Guide

This guide will help you set up and contribute to the Karaoke Jukebox project.

## Project Overview

Karaoke Jukebox is a mobile-first web application for group karaoke parties. Users search YouTube for karaoke videos, queue songs, and the app plays them through Chromecast with real-time queue updates.

**Key Features:**
- YouTube karaoke video search
- Collaborative song queue
- Chromecast playback with automatic progression
- Real-time queue updates via Server-Sent Events (SSE)
- Admin controls for queue management and device control
- Mobile-first responsive design

## Prerequisites

### System Requirements

- **Python 3.13** or later
- **ffmpeg** - Required for video processing
  ```bash
  # macOS
  brew install ffmpeg

  # Ubuntu/Debian
  sudo apt-get install ffmpeg

  # Windows
  # Download from https://ffmpeg.org/download.html
  ```
- **uv** - Python dependency management
  ```bash
  # Standalone installer
  curl -LsSf https://astral.sh/uv/install.sh | sh

  # Or via Homebrew
  brew install uv
  ```

### API Keys

You'll need a **YouTube Data API v3 key**:
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project
3. Enable YouTube Data API v3
4. Create credentials (API key)

## Getting Started

### 1. Clone and Install

```bash
# Clone the repository
git clone <repository-url>
cd karaoke-jukebox

# Install dependencies (runtime + dev tools by default)
uv sync
```

### 2. Environment Configuration

Create `.env` with the following variables (or run `./setup.sh`, which creates it from `.env.example` and generates a `SECRET_KEY`):

```bash
# Required
YOUTUBE_API_KEY=your_youtube_api_key_here
ADMIN_PASSWORD=your_admin_password
SECRET_KEY=your_secret_key_here

# Optional (auto-detected in dev mode)
SERVER_HOST=192.168.1.100  # Your local network IP
SERVER_PORT=8000
```

Generate a secure `SECRET_KEY`:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### 3. Run the Development Server

From the repository root, use any of:

```bash
# Make target
make run

# Shell script
./run.sh

# Directly with uv
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The application will be available at `http://localhost:8000`

## Architecture

### Technology Stack

| Layer | Technology |
|-------|-----------|
| **Backend** | FastAPI, Python 3.13, aiosqlite |
| **Frontend** | Jinja2 templates, HTMX, DaisyUI (Tailwind CSS) |
| **Real-time** | Server-Sent Events (SSE) |
| **Media** | yt-dlp, pychromecast |
| **Deployment** | Gunicorn with Uvicorn workers |

### Project Structure

The application code lives at the repository root under `app/`.

```
.
├── app/
│   ├── main.py              # FastAPI app (ASGI entry point: app.main:app)
│   ├── config.py            # Configuration and settings
│   ├── database.py          # SQLite async operations
│   ├── routes/              # API endpoints
│   │   ├── auth.py          # Authentication
│   │   ├── search.py        # YouTube search
│   │   ├── queue.py         # Queue operations + SSE
│   │   └── admin.py         # Admin controls
│   ├── services/            # Business logic
│   │   ├── youtube.py       # YouTube API integration
│   │   ├── download.py      # Video downloads
│   │   ├── chromecast.py    # Chromecast control
│   │   └── queue_manager.py # Queue state + SSE broadcasting
│   ├── templates/           # Jinja2 + HTMX templates
│   └── static/              # CSS and JS assets
├── tests/                   # pytest test suite (pytest is the test runner)
├── data/
│   ├── videos/              # Downloaded video files
│   └── karaoke.db           # SQLite database
├── .github/workflows/       # CI (ci.yml, yt-dlp-canary.yml)
├── Makefile                 # Developer task runner (run, test, canary, build)
├── run.sh                   # Dev server launcher
├── setup.sh                 # First-time environment setup
├── Dockerfile               # Production container
├── docker-compose.yml       # Compose deployment
├── pyproject.toml           # Dependencies (project + dev group) and tooling config (ruff, pytest)
├── uv.lock                  # Committed lockfile for reproducible installs (dev source of truth)
└── requirements.txt         # Generated from uv.lock (uv export); installed by the Docker image
```

### Key Architectural Patterns

#### 1. Hybrid Threading Model

The app uses both asyncio and threading:
- **AsyncIO**: FastAPI routes, database operations, SSE connections
- **Threading**: Chromecast playback (pychromecast is synchronous)

Communication between threads uses `asyncio.new_event_loop()` to bridge sync/async code.

#### 2. Server-Sent Events (SSE)

Real-time queue updates use SSE instead of WebSockets:
- Each client connection gets a dedicated `asyncio.Queue`
- Broadcasting renders personalized HTML per client (admin vs. user views)
- 30-second heartbeat prevents connection timeouts
- Multiline HTML requires SSE-compliant formatting (`data:` prefix per line)

#### 3. Chromecast State Machine

A background daemon thread manages playback:
- Thread-safe state access via `threading.Lock`
- Event-based signaling for skip/stop requests
- Continuous monitoring of playback state
- Automatic progression through queue

**Critical**: After starting media, wait 500ms before checking status to allow Chromecast to update from previous state.

#### 4. Session Management

Stateless cookie-based sessions:
- Signed cookies using `itsdangerous` (prevents tampering)
- Stores username and admin flag
- 24-hour expiry
- Single admin account (username: "admin")

### Database Schema

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
    status TEXT DEFAULT 'queued'
        CHECK(status IN ('queued', 'playing', 'completed'))
);
```

**Note**: Multiple users can queue the same video (no unique constraint on `video_id`). Duplicates are prevented per-user in application logic.

## Development Workflow

### Running the App

```bash
# Development mode with auto-reload
make run
# or: ./run.sh
# or: uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Access the application
# User view: http://localhost:8000
# Admin login: username=admin, password=<ADMIN_PASSWORD from .env>
```

### Code Quality

```bash
# Lint code
uv run ruff check .

# Format code
uv run ruff format .

# Run both before committing (or use: make lint)
uv run ruff format . && uv run ruff check .
```

### Adding a New Feature

1. **Create route handler** in `app/routes/`
   ```python
   from fastapi import APIRouter, Depends
   from app.routes.auth import require_session

   router = APIRouter()

   @router.get("/my-endpoint")
   async def my_handler(session: dict = Depends(require_session)):
       # Your logic here
       pass
   ```

2. **Include router** in `app/main.py`
   ```python
   from app.routes import my_module
   app.include_router(my_module.router)
   ```

3. **For queue changes**, broadcast updates:
   ```python
   from app.services.queue_manager import queue_manager

   # After modifying queue
   await queue_manager.broadcast_queue_update()
   ```

4. **For HTMX endpoints**, return partial HTML:
   ```python
   from fastapi.templating import Jinja2Templates

   templates = Jinja2Templates(directory="app/templates")

   return templates.TemplateResponse(
       "partials/my_component.html",
       {"request": request, "data": data}
   )
   ```

### Running Tests

The test suite lives in `tests/` and uses **pytest**.

```bash
# Fast unit suite (no network, no secrets required)
uv run pytest
# or: make test

# yt-dlp canary: opt-in live integration test that downloads a known-good
# clip and validates it. Skipped by default because it hits the network.
uv run pytest -m integration --run-integration
# or: make canary
```

The canary guards against YouTube changes that break `yt-dlp` downloads. A daily
CI job (`.github/workflows/yt-dlp-canary.yml`) runs the same check, and
`make build` runs `make preflight` (yt-dlp bump + canary) before building the
Docker image.

## Docker Deployment

### Building

```bash
# From the repository root
docker build -t karaoke-jukebox .
```

### Running

```bash
docker run -d \
  -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  -e ADMIN_PASSWORD=your_password \
  -e YOUTUBE_API_KEY=your_key \
  -e SECRET_KEY=your_secret \
  -e SERVER_HOST=192.168.1.100 \  # CRITICAL: Your Docker host IP
  karaoke-jukebox
```

**Important**: `SERVER_HOST` must be the Docker host's IP address, not the container IP. Chromecast needs to access video files over your local network.

## Common Issues

### Chromecast Not Found

- Ensure firewall allows mDNS (port 5353)
- Disconnect any existing Chromecast connection before scanning
- Check device is on same network

### Videos Won't Play on Chromecast

- Verify `SERVER_HOST` is correctly set (especially in Docker)
- Test URL accessibility: `http://{SERVER_HOST}:8000/data/videos/{video_id}.mp4`
- Ensure ffmpeg is installed

### Queue Not Updating

- Check browser console for SSE connection errors
- Verify `/queue/sse` endpoint is accessible
- Safari has stricter SSE requirements (test in Chrome/Firefox)

### ffmpeg Not Found

```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt-get install ffmpeg
```

### Session Issues

Changing `SECRET_KEY` invalidates all existing sessions. Users will need to log in again.

## Contributing Guidelines

1. **Follow existing code style** - Run `ruff format` (or `make lint`) before committing
2. **Run the tests** - `uv run pytest` (or `make test`) before committing
3. **Keep HTMX patterns** - Frontend uses HTMX, not JavaScript frameworks
4. **Document complex logic** - Especially threading/async interactions
5. **Update this guide** - If you change architecture or setup

## Key Dependencies

### Runtime

- `fastapi` - Web framework
- `uvicorn` - ASGI server
- `gunicorn` - Production server
- `pychromecast` - Chromecast control
- `zeroconf` - Device discovery
- `yt-dlp` - Video downloads
- `google-api-python-client` - YouTube API
- `aiosqlite` - Async SQLite
- `jinja2` - Template engine
- `htmx` - Frontend interactivity (CDN)
- `daisyui` - UI components (CDN)

### Development

- `ruff` - Linting and formatting
- `pytest` - Test runner

See `pyproject.toml` for the complete dependency list (`[project].dependencies` and
the `[dependency-groups].dev` group), with `uv.lock` pinning exact versions. Python
3.13 is pinned via `.python-version`.

