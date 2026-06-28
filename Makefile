# Karaoke Jukebox - developer task runner
#
# Gate philosophy:
#   yt-dlp is the most fragile dependency (YouTube changes break downloads).
#   The "canary" is a live integration test that downloads a known-good clip
#   from YouTube. Because it hits the live network it is OPT-IN and is NEVER
#   run inside `docker build`. Instead it gates deploys: `make build` depends on
#   `make preflight`, which bumps yt-dlp and runs the canary first. If the
#   canary fails, the build does not run. A daily CI canary provides the same
#   signal on a schedule (a red run means: YouTube changed, bump yt-dlp).

.PHONY: test canary preflight build lint run requirements

# Fast unit tests - skips the network canary, no secrets required.
test:
	uv run pytest

# Optional local helper: write a requirements.txt from the uv lockfile (e.g. to
# inspect the resolved runtime set). NOT needed for Docker - the image's `export`
# stage generates its own from uv.lock at build time. The file is gitignored.
requirements:
	uv export --frozen --no-dev --no-hashes --no-emit-project -o requirements.txt

# Live integration test: downloads a known-good clip and validates it.
canary:
	uv run pytest -m integration --run-integration

# Deploy gate: pull the latest yt-dlp (updating uv.lock), then prove downloads
# still work. The Docker build regenerates requirements.txt from uv.lock itself,
# so the yt-dlp bump reaches the image without a separate export step here.
preflight:
	uv sync --upgrade-package yt-dlp
	$(MAKE) canary

# Build the image only if preflight (yt-dlp bump + canary) passed.
build: preflight
	docker build -t karaoke-jukebox .

# Lint and format check.
lint:
	uv run ruff check .
	uv run ruff format --check .

# Local development server (matches the ASGI app object used in the Dockerfile).
run:
	uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
