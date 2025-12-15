# Karaoke Jukebox - Docker Image
# Multi-stage build for optimized production image

FROM python:3.13-slim as base

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
# - SERVER_PORT: External port for Chromecast access (default: 5051)
# - DATA_DIR: Data directory path (default: /app/data)
# - LOG_LEVEL: Logging level (default: INFO)

# Install system dependencies
# - ffmpeg: Required for yt-dlp video downloads and processing
# - curl: Useful for health checks
# - unzip for deno install
# NOTE. Set DOCKER_BUILDKIT=1 for caching
RUN --mount=type=cache,target=/var/cache/apt,id=apt_cache \
    --mount=type=cache,target=/var/lib/apt,id=apt_lists \
    apt-get update && apt-get install -y --no-install-recommends \
    unzip ffmpeg curl 

# install deno for yt-dlp
ENV DENO_INSTALL="/usr/local"
ENV PATH="$DENO_INSTALL/bin:$PATH"
RUN curl -fsSL https://deno.land/install.sh | sh -s -- --yes

# Create app directory
WORKDIR /app

# Copy requirements first (for better layer caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install -r requirements.txt

# Copy application code
COPY app/ ./app/

# Create data directory structure
RUN mkdir -p /app/data/videos && \
    chmod -R 755 /app/data


# Expose port
EXPOSE 5051

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:5051/health || exit 1

# Run with gunicorn using uvicorn workers
# - bind to 0.0.0.0:5051 to accept external connections
# - 4 workers (adjust based on CPU cores)
# - uvicorn worker class for async support
# - timeout 120s for long-running operations (video downloads)
# - access log to stdout
CMD ["gunicorn", \
     "app.main:app", \
     "--bind", "0.0.0.0:5051", \
     "--workers", "1", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--log-level", "info"]
