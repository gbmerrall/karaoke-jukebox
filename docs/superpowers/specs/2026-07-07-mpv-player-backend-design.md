# MpvPlayer Backend with Idle Screensaver - Design

Date: 2026-07-07
Status: Approved design, pending implementation plan

## Context

The playback-backend-abstraction refactor (see
`2026-07-05-playback-backend-abstraction-design.md`, merged at c49366a) split the old
Chromecast monolith into a device-independent `PlayoutService` and a `Player` protocol
with one backend, `ChromecastPlayer`. This spec adds the second backend: `MpvPlayer`,
for the portable Raspberry Pi + projector + USB audio deployment, driving a local HDMI
output via libmpv/DRM (no desktop session).

A working command-line prototype on the target Pi hardware validated the mpv
configuration: `vo=drm`, `drm_device=/dev/dri/card1`, `drm_connector=HDMI-A-2`,
`drm_mode=1280x720`, `hwdec=v4l2m2m`, `idle=yes`, plus an idle-loop "screensaver"
video started by a 15-second timer after playback ends. The prototype's playout logic
is illustrative only; this design maps the validated mpv configuration onto the app's
existing Player contract.

## Goals

- `MpvPlayer` implementing the existing `Player` protocol, playing downloaded videos
  from the local filesystem through libmpv to the DRM/HDMI output.
- Idle screensaver: a configured video loops on screen whenever nothing has played for
  15 seconds, for the entire app lifetime (from boot, between songs, and after the
  admin stops playback). The screen never sits black while the app is up.
- Backend selection via a `PLAYER_BACKEND` env var (`chromecast` default | `mpv`),
  read once at startup by a factory. Existing deployments are unchanged by default.
- Fix the known seam gotcha: `PlayoutService.start_playback` currently refuses to
  start unless `selected_device_uuid` is set, which would permanently block a
  device-less backend.

## Non-Goals

- Admin customisation UI for mpv options (video out, audio device, scaling, hwdec).
  This phase hardcodes the prototype's validated values; the admin pass replaces them.
- Runtime backend toggle in the admin section. The factory is the seam a toggle can
  slot into later; only the env var exists now.
- Audio-device selection. mpv uses its default sink this phase.
- Idle/screensaver content on the Chromecast backend (it has a native backdrop).
- Raspberry Pi provisioning/deployment docs.

## Decisions (from brainstorming)

1. **Backend selection: env var now, toggle later.** `PLAYER_BACKEND` is a property of
   the hardware the app runs on, not a party-time decision. The factory function is
   shaped so an admin toggle can be layered on later without rework.
2. **Idle scope: app lifetime.** The mpv handle is created at app startup and lives
   until shutdown, outside the playout thread's connect/cleanup cycle. The screensaver
   appears ~15 s after boot, before playback ever starts, and again after it stops.
3. **Idle content: single env-configured file.** `IDLE_VIDEO_PATH` points at one video,
   looped infinitely. Unset or missing: log one warning at startup, never arm idle
   timers, screen stays black when idle. Admin-uploadable content comes later.
4. **mpv options: hardcoded module constants.** The prototype's five options plus
   `idle=yes` are constants in `mpv_player.py`. The admin customisation pass will make
   them configurable; building an env-var surface now would be throwaway.
5. **play() implementation: poll loop + event-recorder callback.** Same wait-loop
   skeleton as `ChromecastPlayer` (200 ms poll of skip/stop events, an ended flag, and
   the `MAX_SONG_DURATION` clock). mpv's `end-file` callback only records how playback
   ended; it never starts or schedules anything (see Idle scheduling for why).

## Architecture

### File layout

```
app/services/players/
├── __init__.py           # Player protocol (+ startup/shutdown), PlaybackOutcome,
│                         #   MAX_SONG_DURATION (existing, extended)
├── chromecast_player.py  # existing backend (gains no-op startup/shutdown)
├── mpv_player.py         # NEW: MpvPlayer + idle screensaver logic
└── factory.py            # NEW: create_player(backend) -> Player
app/services/playout.py   # guard generalized; singleton built via factory;
                          #   startup()/shutdown() delegate to the player
app/config.py             # + player_backend, idle_video_path settings
app/main.py               # lifespan calls playout_service.startup();
                          #   Chromecast config log block gated on backend
```

### Player protocol additions

Two lifecycle methods join the protocol. `connect()`/`cleanup()` keep their existing
meaning of "per playout session"; the new pair means "app lifetime":

```python
def startup(self) -> None:
    """Acquire app-lifetime resources. Called once from the app lifespan,
    before any playback. Must not raise: failures are logged and remembered,
    and connect() subsequently returns False."""

def shutdown(self) -> None:
    """Release app-lifetime resources. Called once at app exit, after the
    playout thread has been joined."""
```

- `ChromecastPlayer`: both are no-ops (its connection is per-session and `cleanup()`
  already releases it).
- `MpvPlayer`: `startup()` creates the persistent mpv handle and arms the idle timer;
  `shutdown()` cancels timers and terminates the handle.
- `PlayoutService` gains thin delegating `startup()` and extends its existing
  `shutdown()` to call `self.player.shutdown()` after joining the playout thread.
- `app/main.py` lifespan calls `playout_service.startup()` right after
  `set_event_loop()`, and the existing shutdown path needs no new call site.

### Factory and backend selection

```python
# app/services/players/factory.py
def create_player(backend: str) -> Player:
    """Instantiate the configured playback backend (lazy imports)."""
    if backend == "chromecast":
        from app.services.players.chromecast_player import ChromecastPlayer
        return ChromecastPlayer()
    if backend == "mpv":
        from app.services.players.mpv_player import MpvPlayer
        return MpvPlayer()
    raise ValueError(f"Unknown PLAYER_BACKEND: {backend}")
```

`playout.py`'s singleton becomes:

```python
playout_service = PlayoutService(create_player(settings.player_backend))
```

Lazy imports mean the Chromecast path never imports `mpv_player.py` (and thus never
needs python-mpv), and vice versa. The settings validator rejects unknown values at
startup (fail fast on .env typos); the factory's `ValueError` is defense in depth.

### start_playback guard generalization (the known gotcha)

```python
# before
if not self.player.selected_device_uuid:
    return {"success": False, "message": "No Chromecast device selected"}

# after
if self.player.supports_discovery and not self.player.selected_device_uuid:
    return {"success": False, "message": "No playback device selected"}
```

MpvPlayer (`supports_discovery = False`) starts playback with no device dance.
Chromecast behavior is unchanged except the message string; affected tests update
their expected string. This is the only deliberate behavior change to existing code
paths.

## MpvPlayer

### Constants (hardcoded this phase)

```python
MPV_OPTIONS = {
    "vo": "drm",
    "drm_device": "/dev/dri/card1",
    "drm_connector": "HDMI-A-2",
    "drm_mode": "1280x720",
    "hwdec": "v4l2m2m",
    "idle": "yes",       # keeps the handle alive with nothing loaded
}
IDLE_DELAY = 15.0        # seconds of nothing playing before the screensaver starts
POLL_INTERVAL = 0.2      # seconds between checks in the play() wait loop
LOAD_TIMEOUT = 10.0      # seconds for mpv to confirm a file has loaded (else FAILED)
```

### Construction and startup

- The `mpv` module is imported lazily inside `startup()`, not at module top, so the
  module imports cleanly on machines without libmpv and tests can inject a fake.
- Constructor takes an optional `mpv_module` override for tests:
  `MpvPlayer(mpv_module=None)`; production resolves `import mpv` at startup time.
- `startup()` creates `mpv.MPV(**MPV_OPTIONS)`, registers the `end-file` callback,
  resolves and validates `settings.idle_video_path` (warn once if unset/missing),
  and arms the idle timer if idle content is available. Any exception (libmpv
  missing, DRM device busy, bad connector) is caught and logged; the player marks
  itself unavailable instead of crashing the app, so the admin UI stays reachable
  on the Pi for diagnosis.
- `connect()` returns True iff `startup()` produced a usable handle. The playout
  loop already aborts cleanly (with a logged error) when `connect()` is False.
- `cleanup()` stops the current file but keeps the handle (the screensaver must
  survive the end of a playout session). `shutdown()` cancels timers and calls
  `terminate()` on the handle.

### play(): blocking playback of one song

1. Resolve `settings.get_video_path(video_id)`. Missing file: return `FAILED`
   immediately, before touching any idle/timer state (so a missing file never
   interrupts a running screensaver).
2. Under the state lock: mark "song play in progress", cancel any pending idle
   timer. Then set `loop_file = "no"` and load the song (replacing the idle loop
   if it is on screen).
3. **Load phase.** Loading a song over the screensaver makes mpv fire an
   `end-file` event *for the idle file*, and python-mpv's end-file event does not
   reliably carry the filename across libmpv versions - so end events recorded
   before the song is confirmed loaded must not be trusted (same family of
   problem as the Chromecast stale-status delay). Poll every `POLL_INTERVAL`
   until mpv's `path` property equals the song's path:
   - `stop_event` / `skip_event` set: handle exactly as in the playback phase.
   - `LOAD_TIMEOUT` exceeded (corrupt file, mpv rejected it): stop mpv, return
     `FAILED`.
   Once the path matches, discard any end event recorded so far.
4. **Playback phase.** Poll every `POLL_INTERVAL`:
   - `stop_event` set: stop mpv, return `STOPPED` (never clear the event).
   - `skip_event` set: stop mpv, clear the event, return `SKIPPED`.
   - ended flag set by the `end-file` callback: `FINISHED` for eof, `FAILED` for
     error reasons.
   - wall clock past `MAX_SONG_DURATION`: stop mpv, return `TIMED_OUT`.
5. On every exit path after step 1 (in a `finally`): clear "song play in
   progress" and arm the idle timer, then return the outcome. Any unexpected
   exception is caught and returned as `FAILED` (the contract's
   backends-catch-their-own-errors rule).

`MAX_SONG_DURATION` applies only to songs; the idle loop is infinite by design and
is never played through `play()`.

### Idle scheduling (screensaver)

Design rule: **the `end-file` callback records; it never schedules.** mpv fires
`end-file` events on its own event thread, including for file *replacements* (loading
a song over the idle loop ends the idle file), and the exact reason codes vary by
libmpv version (bytes vs str vs enum). Arming timers from that callback invites races
between mpv's event thread and the playout thread. Instead:

- The callback normalizes the event (reason to a lowercase str, handling bytes/str/
  enum forms) and records "playback ended, reason X" for the wait loop. Nothing else.
- Idle timers are armed only from code we control: at the end of `startup()`, in
  `play()`'s `finally`, and in `cleanup()`. They are cancelled at the start of
  `play()`. All timer state lives behind one `threading.Lock`.
- When the timer fires, `_start_idle()` checks "song play in progress" under the
  lock and does nothing if a song is starting (closes the race where the timer
  fires just as `play()` begins). Otherwise: `loop_file = "inf"`, load the idle
  video.
- If `IDLE_VIDEO_PATH` is unset or the file is missing, timers are never armed and
  the screen stays black when idle (graceful degrade, warned once at startup).

Timeline sanity check: song ends -> `play()` returns and arms the 15 s timer ->
`INTER_SONG_PAUSE` (1 s) -> next `play()` cancels it. The screensaver never flashes
between queued songs; it only appears when the queue is genuinely empty or playback
is stopped.

### Protocol stubs

`supports_discovery = False`, `selected_device_uuid = None`,
`discover_devices()` returns `[]`, `select_device()` returns `False`.

## Configuration

Two new settings in `app/config.py`:

```python
player_backend: str = "chromecast"   # validated: chromecast | mpv
idle_video_path: Optional[Path] = None  # mpv screensaver video; None = disabled
```

- `.env.example` documents `PLAYER_BACKEND` and `IDLE_VIDEO_PATH` with comments.
- `app/main.py`: the "SERVER CONFIGURATION FOR CHROMECAST" startup log block is
  gated on `player_backend == "chromecast"` (it is noise on a Pi and the URL it
  prints is meaningless for local file playback).

## Dependencies

`python-mpv` becomes an optional extra:

```toml
[project.optional-dependencies]
mpv = ["python-mpv"]
```

- Pi install: `uv sync --extra mpv` plus `apt install libmpv2` (system library).
- Docker image and dev Macs: unchanged, never import mpv (lazy factory + lazy
  module import).

## Error handling summary

| Failure | Behavior |
|---|---|
| Unknown `PLAYER_BACKEND` value | Settings validator fails startup with a clear message (fail fast on typos) |
| mpv init fails (no libmpv, DRM busy, bad connector) | `startup()` logs, marks unavailable; app stays up; `connect()` returns False; playout aborts with a clear log |
| Song file missing on disk | `play()` returns `FAILED`; existing retry/give-up policy applies |
| mpv reports an error ending | `FAILED` via the ended flag |
| Idle video unset/missing | One startup warning; screensaver disabled; black screen when idle |
| Exception anywhere in `play()` | Caught, returned as `FAILED` |

## Testing

Same pattern as the refactor: fakes at the seam, full outcome-table coverage.

**`tests/test_mpv_player.py`** (new) - a `FakeMpv` module/handle injected via the
constructor, with controllable end-file events:
- Outcome mapping: eof -> `FINISHED`; skip -> `SKIPPED` and skip_event cleared;
  stop -> `STOPPED` and stop_event NOT cleared; mpv error reason -> `FAILED`;
  missing file -> `FAILED` (without touching timer state); load never confirmed
  within `LOAD_TIMEOUT` -> `FAILED`; duration cap -> `TIMED_OUT`.
- Load phase: an end event recorded before the song's path is confirmed (the
  replaced idle file ending) is discarded and does not produce an outcome;
  skip/stop during the load phase are honored.
- Idle behavior: timer armed after `startup()` and after each `play()`; cancelled
  by the next `play()`; `_start_idle` sets `loop_file="inf"` and loads the idle
  path; timer firing during a starting song does nothing; no timers when
  `idle_video_path` is unset; `cleanup()` keeps the handle and re-arms the timer;
  `shutdown()` cancels timers and terminates the handle.
- Resilience: `startup()` swallowing an mpv init failure; `connect()` False
  afterwards; end-file reasons as bytes and str both decode.

**`tests/test_players.py` / `tests/test_playout.py`** (extended):
- Factory returns the right backend per value and raises on unknown.
- `start_playback` with a `supports_discovery=False` fake player starts without a
  selected device; with the discovery-backed fake it still refuses (new message
  string).
- `PlayoutService.startup()`/`shutdown()` delegate to the player (shutdown after
  thread join).

**Existing tests**: only the guard-message string assertions change.

**Manual gate (on the Pi)**: boot -> screensaver within ~15 s; queue two songs ->
start playback -> screensaver replaced by song; between songs no screensaver flash;
skip works; stop -> screensaver returns after 15 s; empty queue during playback ->
screensaver appears, adding a song replaces it; app shutdown releases the display.

## Sequencing

Additive first, wiring last (strangler-fig, as before):

1. **Contract + policy changes**: protocol `startup()`/`shutdown()`, Chromecast
   no-ops, `PlayoutService.startup()`/`shutdown()` delegation, guard
   generalization, settings, factory. Tests for all.
2. **MpvPlayer**: `mpv_player.py` with idle logic + `tests/test_mpv_player.py`.
3. **Wiring**: singleton via factory, lifespan `startup()` call, Chromecast log
   block gating, `.env.example`, optional dependency, docs (CLAUDE.md).
4. **Manual Pi gate** per the checklist above.

## Forward-looking notes (not in scope)

- The admin customisation pass replaces `MPV_OPTIONS` constants with persisted
  settings surfaced in the admin UI: video output (HDMI-A-1/2), audio device
  (USB), scaling/mode, hwdec. The natural shape is the generalization already
  noted in the refactor spec: the backend enumerates its outputs/options and the
  admin picks - mpv can list DRM connectors and audio devices the same way
  Chromecast lists cast targets.
- A runtime backend toggle, if ever needed, slots into `create_player()` plus
  defined semantics for tearing down one backend and starting the other.
- Idle content could become admin-uploadable (or a directory/playlist) once the
  admin pass exists.
