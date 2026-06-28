# Karaoke Jukebox - Docker Image
# Native pip build. Dependencies are installed from requirements.txt, which is
# a generated artifact exported from the uv lockfile (the dev source of truth):
#   uv export --frozen --no-dev --no-hashes --no-emit-project -o requirements.txt
# `make preflight` regenerates it after bumping yt-dlp, so the image stays in
# sync with uv.lock. uv itself is NOT needed at image build or run time.

FROM python:3.13-slim AS base

# Set Python environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Required runtime environment variables (pass via -e or docker-compose):
# - ADMIN_PASSWORD: Admin user password (required)
# - YOUTUBE_API_KEY: YouTube Data API v3 key (required)
# - SECRET_KEY: Session signing key - generate with: python -c "import secrets; print(secrets.token_hex(32))"
# - SERVER_HOST: Docker host IP address (REQUIRED for Chromecast, e.g., 192.168.1.100)
# - SERVER_PORT: External port for Chromecast access (default: 8000)
# - DATA_DIR: Data directory path (default: /app/data)
# - LOG_LEVEL: Logging level (default: INFO)

# Install system dependencies
# - ffmpeg: Required for yt-dlp video downloads and processing
# - curl: Useful for health checks and the deno installer
# - unzip: required by the deno install script
# NOTE. Set DOCKER_BUILDKIT=1 for caching
RUN --mount=type=cache,target=/var/cache/apt,id=apt_cache \
    --mount=type=cache,target=/var/lib/apt,id=apt_lists \
    apt-get update && apt-get install -y --no-install-recommends \
    unzip ffmpeg curl

# install deno for yt-dlp (the EJS JS-challenge solver runs on deno at runtime)
ENV DENO_INSTALL="/usr/local"
ENV PATH="$DENO_INSTALL/bin:$PATH"
RUN curl -fsSL https://deno.land/install.sh | sh -s -- --yes

# Create app directory
WORKDIR /app

# Copy requirements first (for better layer caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install -r requirements.txt

# Self-healing yt-dlp layer:
# YouTube changes break yt-dlp periodically. Pull the latest yt-dlp on every
# image rebuild so a fresh build always ships a current downloader, even if
# requirements.txt has drifted behind upstream.
RUN pip install --no-cache-dir -U "yt-dlp[default]"

# Copy application code
COPY app/ ./app/

# Create a non-root user and give it ownership of the data directory.
# gunicorn runs as this user (never root) for defense in depth.
RUN useradd --create-home --uid 10001 jukebox && \
    mkdir -p /app/data/videos && \
    chown -R jukebox:jukebox /app/data && \
    chmod -R 755 /app/data

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Drop privileges before running the server
USER jukebox

# Run with gunicorn using uvicorn workers
# - bind to 0.0.0.0:8000 to accept external connections
# - 1 worker (Chromecast playback state lives in-process; do not scale blindly)
# - uvicorn worker class for async support
# - timeout 120s for long-running operations (video downloads)
# - access log to stdout
CMD ["gunicorn", \
     "app.main:app", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "1", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--log-level", "info"]
