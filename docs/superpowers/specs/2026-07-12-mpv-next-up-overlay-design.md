# mpv "Up Next" Overlay — Design

Date: 2026-07-12
Status: Approved, pending implementation

## Context

The mpv backend (`app/services/players/mpv_player.py`) drives local HDMI playback
via a persistent libmpv handle. Graeme has a working command-line prototype that
uses a `time-remaining` property observer plus `MPV.show_text()` to flash a
message near the end of a clip:

```python
@player.property_observer("time-remaining")
def on_remaining(_name, value):
    global shown
    if value is not None and not shown and value <= 15:
        shown = True
        player.show_text("Up next: ...", duration=15000)
```

This design ports that pattern into the app so the on-screen display names the
next queued song and its owner, using the bundled `data/Roboto-Regular.ttf` font.
If the currently playing song is last in the queue, nothing is shown.

## Decisions

1. **`Player.play()` gains a 4th optional parameter, `next_up_text: Optional[str]
   = None`.** `playout.py` — which already holds the full queue — computes the
   display string and passes it in. `ChromecastPlayer` accepts and ignores it
   (Chromecast has no overlay capability). This keeps the existing "backends
   hold no queue knowledge" boundary (`app/services/players/__init__.py`)
   intact in spirit: mpv still never sees a queue id, a title, or the database
   directly — only an opaque string to display.
2. **Overlay text is `"Up next: {title} — for {username}"`.** The queue table
   already has both columns on every row (pilot mode and normal mode alike).
3. **Trigger: 15 seconds of `time-remaining`, shown for 15 seconds** — matches
   the prototype exactly (fires once when `time-remaining` first drops to
   <=15, `show_text(..., duration=15000)`).
4. **Font path is resolved via settings, not mpv's cwd.** The prototype uses
   `osd_fonts_dir="."`, which only works if the process's cwd happens to be
   `data/`. The app instead points `osd_fonts_dir` at the absolute `data/`
   directory, matching how `IDLE_VIDEO_PATH` is already resolved through
   `Settings`.
5. **Last song in queue → no overlay.** `next_up_text` is `None` whenever
   there is no second queue row; the observer callback does nothing when
   `next_up_text` is `None`.

## Design

### Player protocol (`app/services/players/__init__.py`)

```python
def play(
    self,
    video_id: str,
    skip_event: threading.Event,
    stop_event: threading.Event,
    next_up_text: Optional[str] = None,
) -> PlaybackOutcome:
    """... existing docstring, plus:

    Args:
        next_up_text: Optional display string for the next queued song
            (e.g. "Up next: Song — for Alice"), or None if this is the
            last song in the queue. Backends without overlay support
            (e.g. Chromecast) accept and ignore it.
    """
    ...
```

### `ChromecastPlayer.play()` (`app/services/players/chromecast_player.py`)

Signature gains `next_up_text: Optional[str] = None`; the parameter is
accepted and never referenced in the body — no behavior change.

### `playout.py`

`_get_queue_sync`'s SELECT gains `username`:

```python
"SELECT id, video_id, title, username FROM queue "
"WHERE status != 'completed' ORDER BY added_at ASC"
```

In `_playout_loop`, right after `item = queue[0]`:

```python
next_up_text = None
if len(queue) > 1:
    next_item = queue[1]
    next_up_text = f"Up next: {next_item['title']} — for {next_item['username']}"
```

Passed through to the existing `self.player.play(...)` call as the new 4th
positional argument.

### `MpvPlayer` (`app/services/players/mpv_player.py`)

**Config** — `MPV_OPTIONS` gains:

```python
"osd_fonts_dir": str(settings.get_videos_dir().parent),  # data/
"osd_font": "Roboto",
"osd_font_size": 48,
```

(`settings.get_videos_dir()` already resolves to `data/videos/`; its parent is
`data/`, where `Roboto-Regular.ttf` lives today.)

**State** — new instance attributes alongside the existing idle/end-reason
state, all guarded by the existing `_state_lock`:

```python
self._next_up_text: Optional[str] = None
self._next_up_shown = False
```

**Handle construction (`_build_handle`)** — register the property observer
alongside the existing `end-file` callback, once per handle (handles are
rebuilt on `select_output()`, so this cannot live only in `startup()`):

```python
player.property_observer("time-remaining")(self._on_time_remaining)
```

**`play()`** — reset the pair at the top, under the same lock block that
already resets `_song_in_progress`/`_end_reason`, and store the caller's text:

```python
def play(self, video_id, skip_event, stop_event, next_up_text=None):
    ...
    with self._state_lock:
        self._song_in_progress = True
        self._cancel_idle_timer_locked()
        self._end_reason = None
        self._next_up_text = next_up_text
        self._next_up_shown = False
        player = self._player
```

**New callback**, same shape/thread-safety as `_on_end_file` (runs on mpv's
event thread, only touches state under `_state_lock`, never raises):

```python
def _on_time_remaining(self, _name, value) -> None:
    """Show the "up next" overlay once time-remaining drops to the threshold.

    Runs on mpv's event thread. Silent during the idle screensaver
    (_song_in_progress is False then) and whenever next_up_text is None
    (last song in the queue).
    """
    with self._state_lock:
        if not self._song_in_progress or self._next_up_shown:
            return
        text = self._next_up_text
        if text is None or value is None or value > NEXT_UP_THRESHOLD:
            return
        self._next_up_shown = True
        player = self._player
    if player is not None:
        try:
            player.show_text(text, duration=int(NEXT_UP_DURATION * 1000))
        except Exception as e:
            logger.warning(f"Failed to show next-up overlay: {e}")
```

New module constant alongside `IDLE_DELAY`/`POLL_INTERVAL`:

```python
NEXT_UP_THRESHOLD = 15.0  # seconds of time-remaining that triggers the overlay
NEXT_UP_DURATION = 15.0  # seconds the overlay stays on screen
```

### Test fakes

`FakePlayer.play()` in `tests/test_playout.py` gains `next_up_text=None` as an
accepted (and recorded, for assertions) parameter — existing calls without the
kwarg keep working since it's optional.

## Out of scope

- Persisting or making the threshold/duration configurable at runtime (mirrors
  `IDLE_DELAY`'s hardcoded-constant precedent).
- Any overlay support for the Chromecast backend.
- Styling beyond font family/size (color, position, animation) — mpv's
  `show_text` defaults are used as in the prototype.
