# Karaoke Queue App

## Configuration

The app can be configured using environment variables or a `.env` file. Here are the available settings:

### Queue Settings
- `QUEUE_CLEANUP_THRESHOLD_HOURS`: Remove queue items older than this many hours (default: 4)
- `QUEUE_CLEANUP_INTERVAL_HOURS`: Run cleanup job every this many hours (default: 1)
- `MAX_QUEUE_SIZE`: Maximum number of items in queue (default: 0, meaning no limit)

### Admin Settings
- `ADMIN_PASSWORD`: Required for admin access (no default)

Example (Linux/macOS):
```sh
export QUEUE_CLEANUP_THRESHOLD_HOURS=6
export QUEUE_CLEANUP_INTERVAL_HOURS=2
export MAX_QUEUE_SIZE=100
export ADMIN_PASSWORD='your_secret_password'
```

Example (Windows Command Prompt):
```cmd
set QUEUE_CLEANUP_THRESHOLD_HOURS=6
set QUEUE_CLEANUP_INTERVAL_HOURS=2
set MAX_QUEUE_SIZE=100
set ADMIN_PASSWORD=your_secret_password
```

## Admin Login

- The admin username is always `admin` (case-insensitive).
- The admin password is set via the `ADMIN_PASSWORD` environment variable.
- You must set this environment variable before starting the app for admin login to work.

## User Login
- Regular users log in with any name (no password required).

## Other Notes
- There is no admin password stored in the database. All admin authentication is handled via the environment variable.

# Graceful Shutdown for SSE (Server-Sent Events)

When running the app with Uvicorn, you may have long-lived SSE (EventSource) connections open in browsers. By default, Uvicorn may hang on reload or shutdown if these connections are still open. To address this, use the `--timeout-graceful-shutdown` flag:

```
uvicorn src.main:app --reload --timeout-graceful-shutdown 5
```

This gives Uvicorn 5 seconds to gracefully close all open connections (including SSE) before forcefully shutting down. Adjust the timeout as needed for your environment. This helps avoid the need to manually close browser tabs during development reloads and ensures a smoother shutdown process.

# ... (rest of your README) ... 