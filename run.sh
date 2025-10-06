#!/bin/bash
# Development server runner for Karaoke Jukebox

set -e

echo "🎤 Starting Karaoke Jukebox Development Server..."

# Check if .env exists, if not copy from .env.example
if [ ! -f .env ]; then
    echo "⚠️  .env file not found. Please create one from .env.example"
    echo "   cp .env.example .env"
    echo "   Then edit .env with your actual values"
    exit 1
fi

# Check if we're in a virtual environment
if [ -z "$VIRTUAL_ENV" ]; then
    echo "📦 Activating pipenv virtual environment..."
    # Run with pipenv
    cd ..
    pipenv run python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000 --app-dir new
else
    # Already in venv, just run
    python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
fi
