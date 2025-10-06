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

Option 1: Shell script
```bash
./run.sh
```

Option 2: Python script
```bash
python run.py
```

Option 3: From parent directory with pipenv
```bash
cd /Users/graeme/Code/jukebox
pipenv run python new/run.py
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
- All dependencies in `Pipfile` (install with `pipenv install` from parent directory)
- **YouTube Data API v3 key**
- **Chromecast device** on the same network (for playback)

## Docker Deployment

### Build the Image

From the parent directory (`/Users/graeme/Code/jukebox/`):

```bash
docker build -f new/Dockerfile -t karaoke-jukebox .
```

### Run the Container

```bash
docker run -d \
  --name karaoke-jukebox \
  -p 8000:8000 \
  -v $(pwd)/new/data:/app/data \
  -e ADMIN_PASSWORD=your_secure_password \
  -e YOUTUBE_API_KEY=your_youtube_api_key \
  -e SECRET_KEY=your_secret_key \
  -e SERVER_HOST=192.168.1.100 \
  -e SERVER_PORT=8000 \
  karaoke-jukebox
```

**Important Docker Configuration:**

1. **SERVER_HOST** is REQUIRED when running in Docker
   - Set it to your Docker host's IP address (find with `ip addr` or `ifconfig`)
   - DO NOT use `localhost` or `127.0.0.1` (Chromecast cannot reach the container)
   - Example: `SERVER_HOST=192.168.1.100`

2. **Port Mapping**: If you map to a different external port (e.g., `-p 80:8000`):
   - Set `SERVER_PORT` to the EXTERNAL port that Chromecast will use
   - Example: `docker run -p 80:8000 ... -e SERVER_PORT=80 ...`

3. **Data Volume**: Mount `/app/data` to persist videos and database:
   - `-v /path/to/data:/app/data`

4. **Environment Variables**: All required `.env` values should be passed via `-e` flags

### Docker Compose (Optional)

Create `docker-compose.yml`:

```yaml
version: '3.8'
services:
  jukebox:
    build:
      context: .
      dockerfile: new/Dockerfile
    ports:
      - "8000:8000"
    volumes:
      - ./new/data:/app/data
    environment:
      - ADMIN_PASSWORD=your_secure_password
      - YOUTUBE_API_KEY=your_youtube_api_key
      - SECRET_KEY=your_secret_key
      - SERVER_HOST=192.168.1.100  # Your host IP
      - SERVER_PORT=8000
      - DATA_DIR=/app/data
    restart: unless-stopped
```

Run with: `docker-compose up -d`

## Development

The app uses:
- **FastAPI** - Backend framework
- **HTMX** - Dynamic UI updates
- **DaisyUI** - UI components (Tailwind CSS)
- **Server-Sent Events (SSE)** - Real-time queue updates
- **yt-dlp** - Video downloads
- **pychromecast** - Chromecast control

## Troubleshooting

### "Module not found" errors
Make sure you're running from the pipenv environment:
```bash
cd /Users/graeme/Code/jukebox
pipenv install
pipenv shell
cd new
python run.py
```

### Can't find Chromecast
- Ensure your Chromecast and server are on the same network
- Check firewall settings
- Try the "Scan for Devices" button in admin controls

### Videos won't download
- **Check if ffmpeg is installed**: Run `ffmpeg -version` in terminal
  - If not installed, see Requirements section above
- Verify your `YOUTUBE_API_KEY` is valid
- Check `data/videos/` directory permissions
- Look at server logs for specific errors
- Common error: "ffmpeg is not installed" - Install ffmpeg to fix
