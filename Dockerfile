# Karaoke Jukebox - Docker Image
# Multi-stage build for optimized production image

FROM python:3.13-slim as base

# Set environment variables
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
# - curl: Useful for health checks
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Copy requirements first (for better layer caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install -r requirements.txt

# Copy application code (preserve package structure)
COPY app/ ./app/

# Create data directory structure
RUN mkdir -p /app/data/videos && \
    chmod -R 755 /app/data


# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run with gunicorn using uvicorn workers
# - bind to 0.0.0.0:8000 to accept external connections
# - 4 workers (adjust based on CPU cores)
# - uvicorn worker class for async support
# - timeout 120s for long-running operations (video downloads)
# - access log to stdout
CMD ["gunicorn", \
     "app.main:app", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "4", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--log-level", "info"]
