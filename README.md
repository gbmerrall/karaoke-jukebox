# Karaoke Jukebox

Mobile-first karaoke jukebox web app with Chromecast support. We used to have a karaoke machine but no matter how many songs
are loaded on, it never has the songs you want. One day, I wen to a bar in Singapore that queued up karaoke songs using YouTube, 
ads and all! So we started using YouTube at home but people keep mashing the wrong buttons and killing queues or playing their song.
So I built this. 

It deliberately uses Chromecasts for playout over the local network. That's what I've got at home so that's what I use. 
Makes it easy to get videos to the TV. 

There is now also an **mpv playback backend** for portable setups (think Raspberry Pi
plugged into a projector in a hall) - see [mpv playout](#mpv-playout-raspberry-pi--local-hdmi) below.

## Feautures
* Mobile first 
* Super simple for users to start. Just enter your name and search/queue
* Uses the YouTube API to search for songs
* Users can only manage their own songs in the queue
* Simple admin login for managing the queue and playback
* Chromecast playout including searching for devices
* mpv playout for local HDMI output (e.g. Raspberry Pi + projector), with an idle
  "screensaver" video loop between songs
* Auto queue advance

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
- **Chromecast device** on the same network (default playback), or a spare HDMI
  output + `libmpv2` for the mpv backend (see mpv playout section)

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

## mpv playout (Raspberry Pi / local HDMI)

**Status: merged and unit-tested; pending final acceptance on real Pi hardware.**
The full test suite covers the backend against a fake mpv, and the mpv options
below were validated on a Pi with a standalone prototype, but the end-to-end
acceptance checklist (screensaver at boot, gapless queue advance, clean display
release on shutdown) has not been signed off on the target hardware yet. Treat
it as beta until then. Chromecast remains the default and is unaffected.

The mpv backend plays downloaded videos straight out of the local filesystem to
an HDMI output via libmpv/DRM - no desktop session, no X/Wayland. When nothing
has played for 15 seconds (before playback starts, between-sets, after a stop),
it loops an idle "screensaver" video so the projector never sits on a black
screen.

### Enabling it

In `.env`:

```bash
# Switch the playback backend (default: chromecast)
PLAYER_BACKEND=mpv

# Optional: video file looped as the idle screensaver. Unset = disabled
# (black screen when idle).
IDLE_VIDEO_PATH=./data/idle.mp4
```

**Where to put the idle video:** anywhere EXCEPT `data/videos/`. That directory
is managed by the cleanup job, which deletes any `.mp4` not referenced by a
queue item after a few hours - your screensaver would disappear mid-party. The
recommended spot is the `data/` root (`./data/idle.mp4`, as above); any other
path outside `data/videos/` works too, since `IDLE_VIDEO_PATH` is just a file
path. Any format/resolution mpv can decode is fine - it plays on a loop, so a
short clip (10-30 seconds) is plenty.

On the Pi (or any Linux box with a free DRM output):

```bash
sudo apt install libmpv2      # the libmpv system library
uv sync --extra mpv           # installs python-mpv (kept out of default installs)
```

python-mpv is an optional dependency (`mpv` extra) and is imported lazily - the
Chromecast/Docker paths never need libmpv installed. Do NOT use Debian's
apt-packaged `python3-mpv` (it is the old 0.x API); the extra pins
`python-mpv>=1.0`.

### Output configuration

The video-output options are currently hardcoded constants in
`app/services/players/mpv_player.py` (`MPV_OPTIONS`): DRM output on
`/dev/dri/card1`, connector `HDMI-A-2`, 1280x720, `v4l2m2m` hardware decoding.
If your hardware differs, edit those constants. Making them admin-configurable
(video out, audio device, scaling) is the planned next phase.

### Testing

```bash
# The mpv backend's unit tests run everywhere - no libmpv, no display needed
uv run pytest tests/test_mpv_player.py -v

# Whole suite (backend selection, playout policy, config validation)
make test
```

Manual acceptance on the Pi (the remaining gate) is the checklist in
`docs/superpowers/plans/2026-07-07-mpv-player-backend.md` (Task 5): screensaver
within ~15s of boot, songs replace it, no screensaver flash between queued
songs, skip/stop behave, screensaver returns ~15s after stop, clean exit
releases the display.

Notes for admins: with mpv there are no devices to scan or select - just start
playback. If mpv fails to initialize (missing libmpv, DRM device busy), the app
stays up and logs the reason; playback start will then report a connection
failure in the logs rather than crashing the server.

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
- **python-mpv** (optional `mpv` extra) - local HDMI playout via libmpv/DRM

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

### mpv backend won't play / black screen
- Check the logs for `mpv initialization failed` - the usual causes are a
  missing `libmpv2` package, the DRM device path or HDMI connector in
  `MPV_OPTIONS` not matching your hardware, or something else (a desktop
  session) holding the display
- Installed python-mpv from apt? Remove it and use `uv sync --extra mpv`
  (the apt package is the incompatible 0.x API)
- Black screen when idle is expected if `IDLE_VIDEO_PATH` is unset or the file
  is missing - the startup log says so explicitly

### Videos won't download
- **Check if ffmpeg is installed**: Run `ffmpeg -version` in terminal
  - If not installed, see Requirements section above
- **yt-dlp requires a JavaScript engine (deno) for its challenge solver**
  - This is already taken care of in the Dockerfile
- Verify your `YOUTUBE_API_KEY` is valid
- Check `data/videos/` directory permissions
- Look at server logs for specific errors
- Common error: "ffmpeg is not installed" - Install ffmpeg to fix

