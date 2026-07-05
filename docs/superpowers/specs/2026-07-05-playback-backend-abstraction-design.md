# Playback Backend Abstraction - Design Spec

Date: 2026-07-05
Status: Approved for planning

## Context and Motivation

Karaoke Jukebox currently plays out exclusively through Chromecast. A portable deployment is
planned: a Raspberry Pi driving a projector directly via mpv (`python-mpv`), with audio over
USB. Rather than forking the project or maintaining parallel branches, playback is being
abstracted behind a `Player` interface in this single codebase, so both output devices share
the search / queue / SSE / download / auth stack and the yt-dlp maintenance pipeline.

This spec covers the refactor only: extracting the seam and proving it with the existing
Chromecast backend. The mpv backend is a follow-up spec that implements the interface defined
here.

## Goals

1. Split `app/services/chromecast.py:_playout_loop()` into two layers:
   - a device-independent playout controller (queue policy, thread lifecycle, DB bridges)
   - a `Player` interface with `ChromecastPlayer` as the first implementation
2. Preserve all current behavior, with one deliberate exception (see Bug Fix below).
3. Leave the seam shaped so an `MpvPlayer` can be added without changing the controller,
   routes, or templates.

## Non-Goals

- No `MpvPlayer` implementation (follow-up spec).
- No `PLAYER_BACKEND` config flag or backend factory. With one backend it is dead code; the
  mpv spec adds it. A module-level singleton wires `ChromecastPlayer` into the controller.
- No Raspberry Pi provisioning, systemd units, audio configuration, or kiosk setup.
- No changes to routes' URL shapes, JSON responses, templates, or the database schema.

## Decisions Made During Design

- **Scope**: refactor only; mpv is a separate spec.
- **Interface shape**: blocking call plus threading events (see below). Callback/observer and
  async interfaces were rejected: both invert or rewrite the proven threading model for no
  current benefit, and pychromecast is synchronous regardless.
- **Bug fix included**: the session-start-timeout path becomes a counted failure instead of an
  infinite retry (see Bug Fix).
- **Timeout enforcement**: the player's wait loop enforces `MAX_SONG_DURATION` (it owns the
  polling); the constant remains in shared code because the limit is policy, not mechanics.

## Architecture

### File layout

```
app/services/
├── playout.py                  # PlayoutService - queue policy + thread lifecycle (NEW)
├── players/
│   ├── __init__.py             # Player protocol + PlaybackOutcome enum (NEW)
│   └── chromecast_player.py    # ChromecastPlayer - device mechanics (EXTRACTED)
└── chromecast.py               # DELETED (contents split between the two above)
```

### PlaybackOutcome enum

The contract between the layers. Every way a song can end has a name:

`FINISHED`, `SKIPPED`, `STOPPED`, `FAILED`, `TIMED_OUT`

### Player protocol (`app/services/players/__init__.py`)

- `connect() -> bool` - called once when the playout thread starts.
- `play(video_id: str, skip_event: threading.Event, stop_event: threading.Event)
  -> PlaybackOutcome` - blocking. The player resolves its own video reference: Chromecast
  builds an HTTP URL via `settings.get_video_url()`; a future mpv backend builds a filesystem
  path. `SERVER_HOST` concerns live entirely inside the Chromecast backend.
- `cleanup() -> None` - called in the playout thread's `finally`; logs and swallows its own
  errors.
- Optional discovery capability: `supports_discovery: bool`, `async discover_devices(timeout)`,
  `select_device(uuid)`. `ChromecastPlayer` implements it. Backends without it set
  `supports_discovery = False`.

### ChromecastPlayer (`app/services/players/chromecast_player.py`)

Receives, verbatim where possible, from the current service:

- `_connect_to_device()` (CastBrowser/zeroconf connect dance)
- the play block: `play_media(url, "video/mp4", stream_type="BUFFERED")`, the 30s
  `session_active_event.wait()`, the 500ms stale-status sleep, and the `idle_reason` decoding
  loop
- `discover_devices()` (AsyncZeroconf scan, including the disconnect-idle-connection-first
  behavior), `select_device()`, and `DiscoveryListener`
- disconnect/quit-app cleanup

Owns the device-mechanics constants: `POLL_INTERVAL` and the 500ms stale-status delay.
(`MIN_PLAY_TIME_BEFORE_IDLE_CHECK` is defined in the current module but never referenced -
dead code, deleted during the move rather than migrated.) Enforces `MAX_SONG_DURATION` in its
wait loop and returns `TIMED_OUT` when exceeded.

Holds no queue knowledge: it never sees a `queue_id`, a title, or the database.

### PlayoutService (`app/services/playout.py`)

Receives from the current service:

- thread lifecycle: `start_playback()`, `stop_playback()`, `skip_current()`, `shutdown()`
- state: `is_playing`, `playout_lock`, `skip_requested` / `stop_requested` events,
  `_failure_counts`
- the outer loop: stop-signal check, empty-queue 5-second poll (playback resumes automatically
  when songs are added), per-item fate decision, 1-second inter-song pause
- the sync-to-async bridges: `_get_queue_sync()`, `_update_status_sync()`,
  `_remove_from_queue_sync()`, and `set_event_loop()`

Owns the policy constants: `MAX_SONG_DURATION` (value), `MAX_PLAYBACK_RETRIES`.

Constructor takes a `Player` instance. A module-level singleton (`playout_service`) wires in
`ChromecastPlayer`.

Discovery/selection admin calls delegate through `PlayoutService` to the player so routes have
a single dependency. `selected_device_uuid` is exposed as a passthrough property for the
`/admin/status` endpoint, which keeps its current JSON shape.

### Caller changes (mechanical)

- `app/routes/admin.py`: import `playout_service` instead of `chromecast_service`; all six
  endpoints (`/devices/scan`, `/devices/select`, `/playback/start`, `/playback/stop`,
  `/playback/skip`, `/status`) keep their URL shapes and JSON responses.
- `app/main.py`: lifespan event-loop injection, shutdown call, and `/health` import move to
  `playout_service`.

## Data Flow and Outcome Mapping

Runtime flow is unchanged: admin route -> `PlayoutService` method -> playout thread ->
`player.play()` blocks -> outcome returned -> fate decision -> DB update via
`run_coroutine_threadsafe` -> SSE broadcast.

The outcome enum replaces the current `should_remove_from_queue` / `playback_failed` boolean
pair:

| ChromecastPlayer observes | Returns | PlayoutService does |
|---|---|---|
| `idle_reason == FINISHED` | `FINISHED` | Remove item, reset failure count |
| Skip event set | `SKIPPED` | Remove item, reset failure count |
| Stop event set | `STOPPED` | Requeue (`status='queued'`), no failure count |
| `idle_reason == ERROR`; other idle reasons (e.g. CANCELLED); `UNKNOWN` player state; exception during play | `FAILED` | Increment failure count; at `MAX_PLAYBACK_RETRIES` (3) mark `completed`; else requeue |
| Media session never starts within 30s | `FAILED` (bug fix - was infinite retry) | Same as above |
| `MAX_SONG_DURATION` exceeded | `TIMED_OUT` | Remove item (matches current behavior) |
| `idle_reason` in (`INTERRUPTED`, `None`) | not an outcome - keep polling | - |

## Bug Fix (deliberate behavior change)

Current behavior (`chromecast.py:341`): when the media session does not start within 30
seconds, the loop `continue`s. The item stays marked `playing`, is retried forever, never
increments the failure count, and can block the queue indefinitely.

New behavior: that path returns `FAILED` and flows through the standard retry cap. After 3
consecutive failures the item is marked `completed` and the queue advances.

This is the only intended behavior change in the refactor. The test that pinned the old
behavior (`test_playout_session_not_started_continues`) is rewritten to assert the new one.

## Error Handling

- `connect()` returns `False`: thread logs, sets `is_playing = False`, exits (unchanged).
- `player.play()` catches its own exceptions and returns `FAILED`. The controller also wraps
  the call defensively so a misbehaving future backend cannot kill the loop.
- `cleanup()` runs in the thread's `finally` and logs/swallows its own errors, as the current
  disconnect path does.
- The `_*_sync` bridges keep their existing 30-second timeouts and log-and-continue behavior.

## Testing

- **Migrate existing tests**: the `test_playout_*` tests in `tests/test_chromecast.py` move to
  target `PlayoutService` with a scripted `FakePlayer` returning canned outcomes. Queue-policy
  assertions no longer need pychromecast mocking. Device-mechanics tests (discovery, select,
  connect failure, sync bridges) move against `ChromecastPlayer` / `PlayoutService` with
  pychromecast mocked as today.
- **Changed test**: `test_playout_session_not_started_continues` is rewritten for the bug fix
  (session timeout returns `FAILED`, respects the retry cap).
- **New tests**: one per `PlaybackOutcome` value through the fate table, plus the retry-cap
  boundary (2 failures requeue; 3rd marks `completed`).
- **Manual gate before merge**: a real Chromecast end-to-end session - play a song through,
  skip one, stop and resume - since CI has no device.
- `make test`, `uv run ruff check .`, and coverage stay green throughout.

## Sequencing

1. Define the `Player` protocol and `PlaybackOutcome` enum.
2. Extract `ChromecastPlayer` - mechanical move, behavior identical except the session-timeout
   fix.
3. Extract `PlayoutService`; wire the singleton; update `admin.py` and `main.py` imports.
4. Migrate and extend tests; delete `app/services/chromecast.py`.
5. Manual Chromecast verification.

## Forward-Looking Notes (for the mpv spec, not this one)

- `MpvPlayer` implements the same protocol: `play()` resolves a filesystem path under
  `data/videos/` and blocks on python-mpv's end-of-file event; no HTTP serving to the player,
  no `SERVER_HOST`.
- Backend selection: `PLAYER_BACKEND` env var and a small factory replace the hardcoded
  singleton wiring. Backend imports become lazy so the Pi does not need pychromecast/zeroconf
  working and dev machines do not need libmpv.
- Admin-configurable mpv options are expected: video output (HDMI 1/2), audio output device,
  scaling, hardware decoding. Chromecast's discover/select is one instance of a more general
  shape - "the backend enumerates its outputs/options and the admin picks" - so the discovery
  capability should generalize into backend-exposed admin options rather than gaining a
  parallel mechanism. The admin UI hides the device-scan panel for backends without discovery
  and renders backend-specific option controls instead.
