# mpv "Up Next" Overlay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When playing via the mpv backend, flash "Up next: `<title>` — for `<owner>`" on screen for the last 15 seconds of the current song, using the bundled Roboto font; show nothing when the current song is last in the queue. Chromecast playback is untouched.

**Architecture:** `Player.play()` gains a 4th optional parameter, `next_up_text: Optional[str] = None`. `playout.py` (which already holds the full queue) computes the display string from the queue's second row and passes it through on every `play()` call. `ChromecastPlayer` accepts and ignores it. `MpvPlayer` stores it per-call and uses a `time-remaining` property observer (mirroring the existing `end-file` callback pattern) to fire `show_text()` once, 15 seconds before the song ends.

**Tech Stack:** Python 3.13, python-mpv (libmpv bindings), pytest with a fake mpv module/handle (no real libmpv or display needed for tests).

## Global Constraints

- Backends without overlay support (Chromecast) accept `next_up_text` and never read it — no behavior change there.
- Overlay text format is exactly `f"Up next: {title} — for {username}"` (em dash, not hyphen).
- Trigger threshold and display duration are both 15 seconds, as hardcoded module constants (matches the existing `IDLE_DELAY` precedent in `mpv_player.py` — no env var, no settings field).
- `osd_fonts_dir` must resolve to the absolute `data/` directory via `settings`, not a relative path (mirrors how `IDLE_VIDEO_PATH` is resolved).
- The overlay must never fire during the idle screensaver loop, and must fire at most once per song.
- No changes to the `PlaybackOutcome` enum, the queue table schema, or any admin/template code.

---

### Task 1: `Player.play()` gains `next_up_text`; Chromecast ignores it

**Files:**
- Modify: `app/services/players/__init__.py`
- Modify: `app/services/players/chromecast_player.py:192-207`
- Test: `tests/test_chromecast_player.py`

**Interfaces:**
- Produces: `Player.play(video_id: str, skip_event: threading.Event, stop_event: threading.Event, next_up_text: Optional[str] = None) -> PlaybackOutcome`. Task 2 (`playout.py`) will call this with the 4th argument; Task 3 (`MpvPlayer`) will implement the display behavior it drives.

- [ ] **Step 1: Update the `Player` protocol's `play()` signature and docstring**

In `app/services/players/__init__.py`, replace the existing `play()` method (lines 65-87) with:

```python
    def play(
        self,
        video_id: str,
        skip_event: threading.Event,
        stop_event: threading.Event,
        next_up_text: Optional[str] = None,
    ) -> PlaybackOutcome:
        """Play one video, blocking until there is an outcome.

        The backend resolves its own video reference from video_id (URL for
        Chromecast, filesystem path for a local player). It must poll the two
        events and return SKIPPED/STOPPED promptly when they are set, clearing
        skip_event (but never stop_event) before returning.

        Args:
            video_id: YouTube video id of a previously downloaded file.
            skip_event: Set by the controller when an admin skips the song.
            stop_event: Set by the controller when playback is stopped.
            next_up_text: Optional display string for the next queued song
                (e.g. "Up next: Song — for Alice"), or None when this is the
                last song in the queue. Backends without overlay support
                (e.g. Chromecast) accept it and never act on it.

        Returns:
            The PlaybackOutcome describing how playback ended. Implementations
            must catch their own exceptions and return FAILED.
        """
        ...
```

- [ ] **Step 2: Write a failing test for Chromecast ignoring `next_up_text`**

In `tests/test_chromecast_player.py`, add after `test_play_finished` (around line 215):

```python
def test_play_accepts_and_ignores_next_up_text():
    """next_up_text is accepted (Player protocol) but has no effect."""
    cast = _make_fake_cast()
    player = _connected_player(cast)
    skip_event, stop_event = _events()
    with patch("app.services.players.chromecast_player.time.sleep", MagicMock()):
        outcome = player.play(
            "dQw4w9WgXcQ", skip_event, stop_event, next_up_text="Up next: Song — for Bob"
        )
    assert outcome is PlaybackOutcome.FINISHED
    cast.play_media.assert_called_once()
```

- [ ] **Step 3: Run the new test to verify it fails**

Run: `uv run pytest tests/test_chromecast_player.py::test_play_accepts_and_ignores_next_up_text -v`
Expected: FAIL with a `TypeError` (unexpected keyword argument `next_up_text`), since `ChromecastPlayer.play()` does not accept it yet.

- [ ] **Step 4: Update `ChromecastPlayer.play()`'s signature**

In `app/services/players/chromecast_player.py`, replace the `play()` signature and docstring (lines 192-207) with:

```python
    def play(
        self,
        video_id: str,
        skip_event: threading.Event,
        stop_event: threading.Event,
        next_up_text: Optional[str] = None,
    ) -> PlaybackOutcome:
        """Cast one video and block until playback ends one way or another.

        Args:
            video_id: YouTube id of a downloaded video in data/videos/.
            skip_event: Set by the controller to skip; cleared here when honored.
            stop_event: Set by the controller to stop; never cleared here.
            next_up_text: Ignored. Chromecast has no on-screen overlay support;
                this parameter exists only to satisfy the shared Player
                protocol so callers can pass it unconditionally.

        Returns:
            The PlaybackOutcome. All exceptions are caught and become FAILED.
        """
```

Leave the method body unchanged. Add `Optional` to the file's `typing` import if it is not already imported (check the top of the file first — `from typing import Optional` alongside any existing typing imports).

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_chromecast_player.py -v`
Expected: All tests PASS, including `test_play_accepts_and_ignores_next_up_text`.

- [ ] **Step 6: Run the full fast suite to confirm no regressions**

Run: `uv run pytest -q`
Expected: All tests PASS (no new failures).

- [ ] **Step 7: Commit**

```bash
git add app/services/players/__init__.py app/services/players/chromecast_player.py tests/test_chromecast_player.py
git commit -m "feat: add next_up_text to the Player protocol (Chromecast ignores it)"
```

---

### Task 2: `playout.py` computes and passes `next_up_text`

**Files:**
- Modify: `app/services/playout.py:266-268` (the `play()` call), `app/services/playout.py:327-343` (`_get_queue_sync`)
- Test: `tests/test_playout.py`

**Interfaces:**
- Consumes: `Player.play(video_id, skip_event, stop_event, next_up_text=None)` from Task 1.
- Produces: `_get_queue_sync()` rows now include a `"username"` key on every row (in addition to the existing `id`, `video_id`, `title`). This is consumed by the `f"Up next: {title} — for {username}"` computation in `_playout_loop`, and is available to any future caller of `_get_queue_sync`.

- [ ] **Step 1: Write failing tests for the new SELECT column and `next_up_text` computation**

In `tests/test_playout.py`, first update the shared `FakePlayer` so its `play()` accepts and records the new parameter. Replace the existing `play()` method (lines 41-43) with:

```python
    def play(self, video_id, skip_event, stop_event, next_up_text=None):
        self.played.append(video_id)
        self.next_up_texts.append(next_up_text)
        return self.outcomes.pop(0)
```

And add `self.next_up_texts = []` to `FakePlayer.__init__` (after the existing `self.played = []` on line 34).

Then add these two tests after `test_finished_removes_item_and_resets_failures` (around line 397, in the "Fate table" section):

```python
def test_next_up_text_includes_title_and_owner_when_second_song_queued():
    """A second queued song's title/owner are passed as next_up_text."""
    player = FakePlayer([PlaybackOutcome.FINISHED])
    service = PlayoutService(player)
    first = _item(1)
    second = {"id": 2, "video_id": "abc123", "title": "Second Song", "username": "Bob"}
    calls = {"n": 0}

    def side_effect():
        calls["n"] += 1
        if calls["n"] == 1:
            return [first, second]
        service.stop_requested.set()
        return []

    with patch.object(service, "_get_queue_sync", side_effect=side_effect):
        _run_loop(service)
    assert player.next_up_texts == ["Up next: Second Song — for Bob"]


def test_next_up_text_none_when_last_song_in_queue():
    """The last (only) song in the queue gets no next_up_text."""
    service, _, _ = _run_one_song(PlaybackOutcome.FINISHED)
    assert service.player.next_up_texts == [None]
```

Then add this test in the "Async DB bridges against a real running loop" section, after `test_sync_bridges_against_real_loop` (around line 529):

```python
async def test_get_queue_sync_includes_username(initialized_db):
    """_get_queue_sync's SELECT includes username (needed for next_up_text)."""
    service = PlayoutService(FakePlayer())
    loop = asyncio.get_running_loop()
    service.set_event_loop(loop)

    await _insert_song(username="alice")

    rows = await asyncio.to_thread(service._get_queue_sync)
    assert rows[0]["username"] == "alice"
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_playout.py::test_next_up_text_includes_title_and_owner_when_second_song_queued tests/test_playout.py::test_next_up_text_none_when_last_song_in_queue tests/test_playout.py::test_get_queue_sync_includes_username -v`
Expected: FAIL — `test_next_up_text_*` fail with `AttributeError`/`AssertionError` (FakePlayer's old `play()` doesn't accept `next_up_text` yet, or `_playout_loop` never computes it); `test_get_queue_sync_includes_username` fails with `KeyError: 'username'`.

- [ ] **Step 3: Update `_get_queue_sync`'s SELECT**

In `app/services/playout.py`, inside `_get_queue_sync` (around line 339), change:

```python
                    cursor = await db.execute(
                        "SELECT id, video_id, title FROM queue "
                        "WHERE status != 'completed' ORDER BY added_at ASC"
                    )
```

to:

```python
                    cursor = await db.execute(
                        "SELECT id, video_id, title, username FROM queue "
                        "WHERE status != 'completed' ORDER BY added_at ASC"
                    )
```

- [ ] **Step 4: Compute and pass `next_up_text` in `_playout_loop`**

In `app/services/playout.py`, inside `_playout_loop` (around lines 258-268), change:

```python
                item = queue[0]
                queue_id = item["id"]
                title = item["title"]

                logger.info(f"Playing: {title}")
                self._update_status_sync(queue_id, "playing")

                try:
                    outcome = self.player.play(
                        item["video_id"], self.skip_requested, self.stop_requested
                    )
```

to:

```python
                item = queue[0]
                queue_id = item["id"]
                title = item["title"]

                next_up_text = None
                if len(queue) > 1:
                    next_item = queue[1]
                    next_up_text = (
                        f"Up next: {next_item['title']} — for {next_item['username']}"
                    )

                logger.info(f"Playing: {title}")
                self._update_status_sync(queue_id, "playing")

                try:
                    outcome = self.player.play(
                        item["video_id"],
                        self.skip_requested,
                        self.stop_requested,
                        next_up_text,
                    )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_playout.py -v`
Expected: All tests PASS.

- [ ] **Step 6: Run the full fast suite to confirm no regressions**

Run: `uv run pytest -q`
Expected: All tests PASS.

- [ ] **Step 7: Commit**

```bash
git add app/services/playout.py tests/test_playout.py
git commit -m "feat: compute and pass next_up_text from the queue's second row"
```

---

### Task 3: `MpvPlayer` shows the overlay via a `time-remaining` observer

**Files:**
- Modify: `app/services/players/mpv_player.py`
- Test: `tests/test_mpv_player.py`

**Interfaces:**
- Consumes: `next_up_text` parameter on `play()` from Task 1; the exact string computed by Task 2.
- Produces: no new public interface — this is the terminal consumer of `next_up_text`.

- [ ] **Step 1: Extend `FakeMpvHandle` to support property observers and `show_text`**

In `tests/test_mpv_player.py`, update `FakeMpvHandle.__init__` (lines 45-53) by adding two lines after `self.handlers = {}`:

```python
        self.handlers = {}
        self.observers = {}
        self.show_text_calls = []  # list of (text, duration) tuples
        self.auto_load = True
```

Add a `property_observer` method and a `show_text` method after `event_callback` (after line 60):

```python
    def property_observer(self, name):
        def decorator(fn):
            self.observers[name] = fn
            return fn

        return decorator

    def show_text(self, text, duration=None):
        self.show_text_calls.append((text, duration))
```

Add a `fire_time_remaining` helper after `fire_end_file` (after line 87), mirroring its docstring style:

```python
    def fire_time_remaining(self, value):
        """Deliver a time-remaining property change (mpv event thread stand-in)."""
        self.observers["time-remaining"]("time-remaining", value)
```

- [ ] **Step 2: Write failing tests for the overlay**

In `tests/test_mpv_player.py`, add a new section at the end of the file (after the "Output selection" section, i.e. after `test_play_after_select_output_uses_the_new_handle` around line 673):

```python
# ---------------------------------------------------------------------------
# "Up next" overlay
# ---------------------------------------------------------------------------


def test_build_handle_registers_time_remaining_observer(make_backend):
    """Every built handle gets a time-remaining observer, like end-file."""
    player, handle = make_backend()
    assert "time-remaining" in handle.observers


def test_osd_font_options_set(make_backend):
    """MPV_OPTIONS carries the bundled Roboto font, resolved via settings."""
    player, handle = make_backend()
    assert handle.options["osd_font"] == "Roboto"
    assert handle.options["osd_font_size"] == 48
    assert handle.options["osd_fonts_dir"] == str(settings.get_videos_dir().parent)


def test_overlay_shown_once_at_threshold(make_backend):
    """The overlay fires once time-remaining first drops to the threshold."""
    player, handle = make_backend()
    _add_video()
    thread, result, _, _ = _start_play_with_next_up(
        player, next_up_text="Up next: Song — for Bob"
    )
    assert player._load_confirmed.wait(2)
    handle.fire_time_remaining(20)
    assert handle.show_text_calls == []
    handle.fire_time_remaining(15)
    assert handle.show_text_calls == [("Up next: Song — for Bob", 15000)]
    handle.fire_time_remaining(10)  # still above zero, must not fire again
    assert handle.show_text_calls == [("Up next: Song — for Bob", 15000)]
    handle.fire_end_file(b"eof")
    thread.join(timeout=2)
    assert result["outcome"] is PlaybackOutcome.FINISHED


def test_overlay_not_shown_when_no_next_song(make_backend):
    """next_up_text=None (last song in queue) never triggers show_text."""
    player, handle = make_backend()
    _add_video()
    thread, result, _, _ = _start_play_with_next_up(player, next_up_text=None)
    assert player._load_confirmed.wait(2)
    handle.fire_time_remaining(5)
    assert handle.show_text_calls == []
    handle.fire_end_file(b"eof")
    thread.join(timeout=2)


def test_overlay_ignores_none_time_remaining(make_backend):
    """A None time-remaining value (no duration data yet) is a no-op."""
    player, handle = make_backend()
    _add_video()
    thread, result, _, _ = _start_play_with_next_up(
        player, next_up_text="Up next: Song — for Bob"
    )
    assert player._load_confirmed.wait(2)
    handle.fire_time_remaining(None)
    assert handle.show_text_calls == []
    handle.fire_end_file(b"eof")
    thread.join(timeout=2)


def test_overlay_silent_when_not_playing_a_song():
    """The observer callback is a no-op outside an active song (e.g. idle)."""
    player = MpvPlayer(mpv_module=FakeMpvModule())
    player.startup()
    handle = player._player
    with player._state_lock:
        player._next_up_text = "Up next: Song — for Bob"
        player._song_in_progress = False
    player._on_time_remaining("time-remaining", 5)
    assert handle.show_text_calls == []
    player.shutdown()
```

Add the `_start_play_with_next_up` helper near the existing `_start_play` helper (after line 173):

```python
def _start_play_with_next_up(player, video_id="vid1", next_up_text=None):
    """Run play() on a worker thread with a next_up_text argument.

    Returns:
        (thread, result_dict, skip_event, stop_event); result_dict['outcome']
        holds the PlaybackOutcome once the thread finishes.
    """
    skip, stop = threading.Event(), threading.Event()
    result = {}

    def worker():
        result["outcome"] = player.play(video_id, skip, stop, next_up_text)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread, result, skip, stop
```

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_mpv_player.py -k "overlay or time_remaining or osd_font" -v`
Expected: FAIL — `test_build_handle_registers_time_remaining_observer` and `test_osd_font_options_set` fail because the observer/options don't exist yet; the overlay tests fail with `KeyError: 'time-remaining'` (no observer registered) or similar.

- [ ] **Step 4: Add the font options and overlay constants**

In `app/services/players/mpv_player.py`, update `MPV_OPTIONS` (lines 39-47) to:

```python
MPV_OPTIONS = {
    "vo": "drm",
    "drm_device": "/dev/dri/card1",
    "drm_connector": "HDMI-A-2",
    "drm_mode": "1280x720",
    "hwdec": "v4l2m2m",
    "audio_device": "alsa/sysdefault:CARD=iBassoDCSeries",
    "idle": "yes",  # keep the handle alive with nothing loaded
    "osd_fonts_dir": str(settings.get_videos_dir().parent),  # data/
    "osd_font": "Roboto",
    "osd_font_size": 48,
}
```

Add two new constants after `LOAD_TIMEOUT` (line 51):

```python
NEXT_UP_THRESHOLD = 15.0  # seconds of time-remaining that triggers the overlay
NEXT_UP_DURATION = 15.0  # seconds the overlay stays on screen
```

- [ ] **Step 5: Add overlay state to `__init__`**

In `app/services/players/mpv_player.py`, in `MpvPlayer.__init__` (around line 105), add two lines after `self._end_reason: Optional[str] = None`:

```python
        self._end_reason: Optional[str] = None  # last recorded end-file reason
        self._next_up_text: Optional[str] = None  # display text for the next song
        self._next_up_shown = False  # whether this song's overlay already fired
```

- [ ] **Step 6: Register the `time-remaining` observer in `_build_handle`**

In `app/services/players/mpv_player.py`, in `_build_handle` (around line 152-153), change:

```python
            player = mpv_module.MPV(**options)
            player.event_callback("end-file")(self._on_end_file)
            return player
```

to:

```python
            player = mpv_module.MPV(**options)
            player.event_callback("end-file")(self._on_end_file)
            player.property_observer("time-remaining")(self._on_time_remaining)
            return player
```

- [ ] **Step 7: Accept and store `next_up_text` in `play()`**

In `app/services/players/mpv_player.py`, update `play()`'s signature (around line 309-314) to:

```python
    def play(
        self,
        video_id: str,
        skip_event: threading.Event,
        stop_event: threading.Event,
        next_up_text: Optional[str] = None,
    ) -> PlaybackOutcome:
```

Add `next_up_text` to the docstring's `Args:` section (after `stop_event`):

```python
            next_up_text: Optional display string for the next queued song
                (e.g. "Up next: Song — for Alice"), or None if this is the
                last song in the queue.
```

Then, in the `with self._state_lock:` block that resets state at the start of `play()` (around lines 337-347), change:

```python
        self._load_confirmed.clear()
        with self._state_lock:
            # Flag first: a concurrent _start_idle() holding the lock finishes
            # its idle load before we proceed, and our song load then wins.
            self._song_in_progress = True
            self._cancel_idle_timer_locked()
            self._end_reason = None
            # Captured under the same lock select_output() holds while
            # swapping self._player, so a handle recreated concurrently with
            # this call is never operated on via a stale, terminated
            # reference (the pre-lock check above is just a fast path).
            player = self._player
```

to:

```python
        self._load_confirmed.clear()
        with self._state_lock:
            # Flag first: a concurrent _start_idle() holding the lock finishes
            # its idle load before we proceed, and our song load then wins.
            self._song_in_progress = True
            self._cancel_idle_timer_locked()
            self._end_reason = None
            self._next_up_text = next_up_text
            self._next_up_shown = False
            # Captured under the same lock select_output() holds while
            # swapping self._player, so a handle recreated concurrently with
            # this call is never operated on via a stale, terminated
            # reference (the pre-lock check above is just a fast path).
            player = self._player
```

- [ ] **Step 8: Add the `_on_time_remaining` callback**

In `app/services/players/mpv_player.py`, add this method after `_on_end_file` (after line 503, before `list_video_outputs`):

```python
    def _on_time_remaining(self, _name, value) -> None:
        """Show the "up next" overlay once time-remaining drops to the threshold.

        Runs on mpv's event thread (like _on_end_file): only touches state
        under _state_lock and never raises. Silent whenever a song is not
        actively playing (e.g. during the idle screensaver, when
        _song_in_progress is False) or when there is no next song
        (next_up_text is None, set by play()'s caller for the last queue row).

        Args:
            _name: Property name ("time-remaining"), unused.
            value: Seconds remaining, or None when mpv has no duration data
                yet (e.g. just after a file starts loading).
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

- [ ] **Step 9: Run the tests to verify they pass**

Run: `uv run pytest tests/test_mpv_player.py -v`
Expected: All tests PASS, including the six new overlay tests.

- [ ] **Step 10: Run the full fast suite to confirm no regressions**

Run: `uv run pytest -q`
Expected: All tests PASS.

- [ ] **Step 11: Run lint**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: No errors.

- [ ] **Step 12: Commit**

```bash
git add app/services/players/mpv_player.py tests/test_mpv_player.py
git commit -m "feat: show an up-next overlay in mpv 15s before a song ends"
```

---

## Final Verification

- [ ] Run the full fast suite once more from the repo root: `uv run pytest -q`. Expected: all tests pass, 0 failures.
- [ ] Run `uv run ruff check . && uv run ruff format --check .`. Expected: clean.
- [ ] Manual walkthrough note (cannot be automated — no libmpv/DRM in this environment): on the Pi, queue two songs and confirm "Up next: `<title>` — for `<owner>`" appears in the last 15 seconds of the first song, in the Roboto font, and confirm queuing only one song shows no overlay at all.
