# Karaoke Jukebox

Mobile-first karaoke jukebox web app with Chromecast support.

## Quick Start

### 1. Setup Environment

```bash
# Run the setup script
./setup.sh
```

This will:
- Create `.env` from `.env.example`
- Generate a secure `SECRET_KEY`
- Prompt you to edit `.env` for required values

### 2. Configure Required Variables

Edit `.env` and set:

```bash
# Admin password (choose something secure)
ADMIN_PASSWORD=your_secure_password

# YouTube API Key (get from https://console.cloud.google.com/apis/credentials)
YOUTUBE_API_KEY=your_youtube_api_key
```

### 3. Run Development Server

Option 1: Shell script (from the repository root)
```bash
./run.sh
```

Option 2: Make target
```bash
make run
```

Option 3: Directly with uv (from the repository root)
```bash
uv sync
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 4. Access the App

Open your browser to: **http://localhost:8000**

## Usage

### Regular Users
1. Enter your name on the login page
2. Search for karaoke songs
3. Queue songs you want to sing
4. See the queue update in real-time

### Admin Users
1. Login with username: `admin` and your admin password
2. Access admin controls to:
   - Scan for Chromecast devices
   - Start/stop playback
   - Skip songs
   - Clear the queue

## Requirements

- **Python 3.13**
- **ffmpeg** - Required for video downloads
  - macOS: `brew install ffmpeg`
  - Ubuntu/Debian: `sudo apt-get install ffmpeg`
  - Windows: Download from https://ffmpeg.org/download.html
- **deno** - Required at runtime for yt-dlp's JS-challenge solver (downloads fail without it)
- **uv** - Python dependency management (install with `curl -LsSf https://astral.sh/uv/install.sh | sh` or `brew install uv`)
- All dependencies in `pyproject.toml` (install with `uv sync` from the repository root; `uv.lock` pins exact versions)
- **YouTube Data API v3 key**
- **Chromecast device** on the same network (for playback)

## Docker Deployment

### Build the Image

```bash
docker build -t karaoke-jukebox .
```

The image is multi-stage. A short `export` stage uses `uv` to generate
`requirements.txt` from the committed `uv.lock`; the runtime stage then
`pip install`s it. `uv.lock` is the single source of truth, `requirements.txt`
is generated at build time (not committed), and `uv` never ends up in the
runtime image.

### Run the Container

```bash
docker run -d \
  --name karaoke-jukebox \
  -p 8000:8000 \
  -e ADMIN_PASSWORD=your_secure_password \
  -e YOUTUBE_API_KEY=your_youtube_api_key \
  -e SECRET_KEY=your_secret_key \
  -e SERVER_HOST=192.168.1.100 \
  karaoke-jukebox
```

**Important Docker Configuration:**

1. **SERVER_HOST** is REQUIRED when running in Docker
   - Set it to your Docker host's IP address (find with `ip addr` or `ifconfig`)
   - DO NOT use `localhost` or `127.0.0.1` (Chromecast cannot reach the container)
   - Example: `SERVER_HOST=192.168.1.100`

2. **SECRET_KEY** must be at least 32 characters
   - Generate with: `python -c "import secrets; print(secrets.token_hex(32))"`

3. **Port Mapping**: If you map to a different external port (e.g., `-p 80:8000`):
   - Set `SERVER_PORT` to the EXTERNAL port that Chromecast will use
   - Example: `docker run -p 80:8000 ... -e SERVER_PORT=80 ...`

4. **Data Persistence**: Database and videos are ephemeral (lost on container restart)
   - To persist data, add: `-v /path/to/data:/app/data`

5. Using DOCKER_BUILDKIT for caching of apt data to speed up rebuilds. Ensure you set DOCKER_BUILDKIT=1 before building

### Docker Compose

A `docker-compose.yml` file is included. Before running:

1. Edit `docker-compose.yml` and set the required environment variables:
   - `ADMIN_PASSWORD`
   - `YOUTUBE_API_KEY`
   - `SECRET_KEY` (generate with: `python -c "import secrets; print(secrets.token_hex(32))"`)
   - `SERVER_HOST` (your Docker host's IP address)

2. Run with:
   ```bash
   docker-compose up -d
   ```

**Note:** Database and videos are ephemeral by default. To persist data, uncomment the volumes section in `docker-compose.yml`

## Keeping downloads working (yt-dlp)

`yt-dlp` is the most fragile part of this app. YouTube periodically changes how
it serves video, which breaks downloads until `yt-dlp` is updated. Downloads
also require **ffmpeg** and **deno** (yt-dlp runs a deno-based JS-challenge
solver) to be installed at runtime.

How this project stays ahead of breakage:

- **Canary test**: an opt-in integration test downloads a known-good clip and
  validates it. It hits the live network, so it is skipped by default.
  ```bash
  make canary            # uv run pytest -m integration --run-integration
  ```
- **Deploy gate**: `make preflight` bumps yt-dlp and then runs the canary.
  `make build` depends on `preflight`, so the Docker image will only build if a
  freshly-updated yt-dlp can still download:
  ```bash
  make preflight         # bump yt-dlp (updates uv.lock) + canary
  make build             # preflight, then docker build
  ```
- **Daily CI canary**: `.github/workflows/yt-dlp-canary.yml` runs the same
  canary on a daily schedule. A red run means YouTube changed and yt-dlp needs a
  bump. The Docker image also force-upgrades yt-dlp on every rebuild, so a plain
  rebuild usually picks up the fix.

To recover from broken downloads: run `make preflight` locally, commit the
updated `uv.lock`, and rebuild the image (the build regenerates
`requirements.txt` from `uv.lock` automatically).

## Development

The app uses:
- **FastAPI** - Backend framework
- **HTMX** - Dynamic UI updates
- **DaisyUI** - UI components (Tailwind CSS)
- **Server-Sent Events (SSE)** - Real-time queue updates
- **yt-dlp** - Video downloads
- **pychromecast** - Chromecast control

### Testing and linting

```bash
make test     # fast unit suite (uv run pytest); the live yt-dlp canary is skipped
make lint     # uv run ruff check . + ruff format --check .
```

See `DEVELOPMENT.md` for the full contributor workflow.

## Troubleshooting

### Can't find Chromecast
- Ensure your Chromecast and server are on the same network
- Check firewall settings
- Try the "Scan for Devices" button in admin controls

### Videos won't download
- **Check if ffmpeg is installed**: Run `ffmpeg -version` in terminal
  - If not installed, see Requirements section above
- **yt-dlp requires a JavaScript engine (deno) for its challenge solver**
  - This is already taken care of in the Dockerfile
- Verify your `YOUTUBE_API_KEY` is valid
- Check `data/videos/` directory permissions
- Look at server logs for specific errors
- Common error: "ffmpeg is not installed" - Install ffmpeg to fix

