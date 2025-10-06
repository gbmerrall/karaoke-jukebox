# Karaoke Jukebox - Docker Image
# Multi-stage build for optimized production image

FROM python:3.13-slim as base

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies
# - ffmpeg: Required for yt-dlp video downloads and processing
# - curl: Useful for health checks
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Install pipenv
RUN pip install pipenv

# Copy Pipfile and Pipfile.lock
# Note: Build context should be the parent directory
# Build with: docker build -f new/Dockerfile -t karaoke-jukebox .
COPY Pipfile Pipfile.lock ./

# Install Python dependencies
# Use --system to install to system python (not virtualenv) since we're in a container
# Use --deploy to ensure Pipfile.lock is up to date
RUN pipenv install --system --deploy --ignore-pipfile

# Copy application code
COPY new/ .

# Create data directory structure
RUN mkdir -p /app/data/videos && \
    chmod -R 755 /app/data

# Create marker file to indicate Docker environment
RUN touch /.dockerenv

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
