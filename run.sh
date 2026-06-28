#!/bin/bash
# Development server runner for Karaoke Jukebox
# Run from the repository root.

set -e

echo "Starting Karaoke Jukebox development server..."

# Check if .env exists, if not tell the user how to create one
if [ ! -f .env ]; then
    echo ".env file not found. Create one from .env.example:"
    echo "   cp .env.example .env"
    echo "   Then edit .env with your actual values (or run ./setup.sh)"
    exit 1
fi

# The ASGI app object is app.main:app (same object the Docker image serves).
# `uv run` resolves and uses the project's managed environment automatically,
# syncing it from uv.lock if needed - no manual venv activation required.
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
