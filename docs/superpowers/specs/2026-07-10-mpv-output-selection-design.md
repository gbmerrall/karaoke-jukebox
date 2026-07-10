# mpv Local Output Selection (Admin) — Design

Date: 2026-07-10
Status: Approved, pending implementation

## Context

`app/services/players/mpv_player.py` hardcodes the Raspberry Pi's local video/audio
output in `MPV_OPTIONS`:

```python
MPV_OPTIONS = {
    "vo": "drm",
    "drm_device": "/dev/dri/card1",
    "drm_connector": "HDMI-A-2",
    "drm_mode": "1280x720",
    "hwdec": "v4l2m2m",
    "audio_device": "alsa/sysdefault:CARD=iBassoDCSeries",
    "idle": "yes",
}
```

On Raspberry Pi's `vc4-kms-v3d` driver, each physical HDMI port is its own DRM card
device (not one card exposing multiple connectors as on a typical desktop GPU), so
`drm_device` and `drm_connector` are a coupled pair describing "one HDMI port" — an
admin picking "the other port" must change both together, never independently.

This spec covers exposing video output (drm_device + drm_connector) and audio
output (audio_device) as admin-selectable, at runtime, without touching
`drm_mode` or `hwdec` (out of scope — no enumeration problem for those today).

## Decisions

1. **Runtime-only, not persisted.** Mirrors the existing Chromecast
   `select_device()` behavior: the choice lives in memory on `MpvPlayer` and
   resets to the hardcoded `MPV_OPTIONS` defaults on every app restart. No new
   settings file, no database table, no `.env` write-back.
2. **Both video and audio output are in scope**, as two independent selections
   (not a single combined choice).
3. **Blocked during active playback.** Changing output while a song is playing
   returns `409` and does nothing. Allowed any time the mpv handle is idle
   (nothing loaded, or the idle screensaver is looping) — screensaver playback
   does not count as "playback" for this guard.
4. **Enumeration is native, no new system dependencies:**
   - Video outputs: walk `/sys/class/drm/card*-*/status` directly in Python
     (pure `pathlib`), keep entries where `status == "connected"`.
   - Audio outputs: query the existing persistent mpv handle's
     `audio_device_list` property (mpv already talks to ALSA; no `aplay`
     shell-out needed).
   - No new packages required in the Dockerfile or on the Pi.
5. **New dedicated interface, not forced into `Player.discover_devices`/
   `select_device`.** That protocol is Chromecast-shaped: one flat list of
   `{name, uuid}` devices, one selection. Video output and audio output are two
   independent axes here, so packing both into a composite "uuid" string would
   be a hack. `MpvPlayer` gets its own methods; `admin.py` gets its own routes;
   `admin.html` gets its own card. The `Player` protocol
   (`app/services/players/__init__.py`) is unchanged.

## Design

### Architecture / data flow

```
Admin UI (admin.html, new "Local Output" card, shown only when player_backend == "mpv")
    |
    +-- GET  /admin/mpv/outputs        -> enumerate live options
    |       calls playout_service.player.list_video_outputs()
    |                                  .list_audio_outputs()
    |
    +-- POST /admin/mpv/output/select  -> apply a choice
            calls playout_service.player.select_output(drm_device, drm_connector, audio_device)
                 -> 409 if a song is currently playing
                 -> else: terminate + recreate the mpv handle with the
                   new drm_device/drm_connector/audio_device, re-arm
                   idle timer, return success
```

### `MpvPlayer` changes (`app/services/players/mpv_player.py`)

- **`list_video_outputs() -> list[dict]`**: walk
  `/sys/class/drm/card*-*/status`, keep entries where
  `status == "connected"`, parse the card number and connector name out of the
  directory name (e.g. `card1-HDMI-A-2` ->
  `{"drm_device": "/dev/dri/card1", "drm_connector": "HDMI-A-2", "label": "HDMI-A-2 (card1)"}`).
  Base sysfs path is overridable (constructor param or module constant) so
  tests can point it at a fake tree under `tmp_path`.
- **`list_audio_outputs() -> list[dict]`**: read
  `self._player.audio_device_list` off the existing persistent handle; return
  `[]` if `self._player is None`.
- **`select_output(drm_device: str, drm_connector: str, audio_device: str) -> tuple[bool, str]`**:
  - Under `_state_lock`: if `_song_in_progress`, return
    `(False, "Cannot change output during playback. Stop or wait for the current song to finish.")`
    without touching the handle.
  - Otherwise: cancel the idle timer, `terminate()` the current handle, build
    a fresh options dict (copy of `MPV_OPTIONS` with `drm_device`,
    `drm_connector`, `audio_device` overridden), construct a new handle via
    the same construction path `startup()` uses (factor into a shared
    private helper, e.g. `_build_handle(options) -> handle | None`, so there
    is exactly one place that calls `mpv_module.MPV(**opts)` +
    registers `event_callback("end-file")`).
  - On success: store the handle, store the new selection as current state,
    re-arm the idle timer, return `(True, "")`.
  - On failure: log, set `self._player = None` (same as a failed `startup()`),
    return `(False, "<error>")`. Local video is down until a fix + restart;
    same risk profile as an existing failed `startup()`.

### Admin routes (`app/routes/admin.py`)

- **`GET /admin/mpv/outputs`** -> `{"video": [...], "audio": [...]}`. Returns
  `404` (or an empty/disabled response) when `settings.player_backend != "mpv"`.
- **`POST /admin/mpv/output/select`**
  (`Form(drm_device: str, drm_connector: str, audio_device: str)`) -> calls
  `playout_service.player.select_output(...)`.
  - Success: `200 {"success": true, "message": "Output updated"}`.
  - Blocked (playback active): `409 {"success": false, "message": "..."}`
    (reuse `select_output`'s returned message).
  - Same JSON response shape as the existing `/admin/devices/select`.

### Admin template (`app/templates/admin.html`)

New card, rendered only when `player_backend == "mpv"` (parallel to the
existing Chromecast-only device-scan card): two `<select>` dropdowns (video
output, audio output) populated via a fetch to `/admin/mpv/outputs` on page
load, one "Apply" button posting to `/admin/mpv/output/select`. The Apply
button is disabled with an explanatory tooltip while a song is playing
(mirrors the `409` case client-side so the admin isn't surprised by a
rejected request).

### Error handling / edge cases

- **Zero connected video outputs** (e.g. both HDMI unplugged): dropdown shows
  "No outputs detected", Apply disabled.
- **Re-selecting the currently-active output**: no special-casing; a
  recreate-with-same-options is idempotent and harmless.
- **Handle recreation failure** (bad connector race, DRM device busy):
  surfaced the same way a failed `startup()` is today — `self._player` is
  `None`, `connect()` returns `False`, the playout thread aborts cleanly, and
  the admin UI stays reachable.

## Testing

- Unit tests for `list_video_outputs()` / `list_audio_outputs()` against a
  fake `/sys/class/drm` tree (`tmp_path` + injected base path) and a fake
  `mpv_module` exposing `audio_device_list`.
- Unit test for `select_output()`:
  - Rejects (returns `False`, handle untouched) while `_song_in_progress`.
  - On success: verifies `terminate()` was called on the old handle, a new
    handle was built with the overridden options, and the idle timer is
    re-armed afterward.
  - On construction failure: `self._player` ends up `None`, matching failed
    `startup()` behavior.
- Route tests for the two new admin endpoints: `404` when backend isn't mpv,
  `409` during playback, `200` + correct JSON body otherwise.

## Out of scope

- Persisting the selection across restarts (explicitly rejected — see
  Decisions).
- `drm_mode` / `hwdec` selection (still hardcoded).
- Any change to the Chromecast backend or the `Player` protocol.
