# Pilot Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a single admin run karaoke sessions solo — search and queue songs on behalf of whoever's turn it is — gated behind a new `PILOT_MODE` setting that also blocks non-admin login while active.

**Architecture:** One new boolean setting (`pilot_mode`) drives three call sites: `POST /login` rejects non-admin usernames, `GET /admin/` passes the flag into the template, and `admin.html` conditionally renders a "Queue a Song" card that reuses the existing `/search` and `POST /queue/{video_id}` endpoints. The queue endpoint grows an optional `owner` field that, for admin callers only, substitutes for the session username when adding to the queue. No changes to `queue_manager.py` — `add_to_queue` already accepts an arbitrary username string.

**Tech Stack:** FastAPI, Jinja2, HTMX, pytest + FastAPI TestClient (existing patterns in `tests/test_routes.py`).

## Global Constraints

- `PILOT_MODE` defaults to `False` (existing installs are unaffected).
- Pydantic-settings parses bare `bool` fields from env vars natively — no custom `field_validator` needed for `pilot_mode` (unlike `player_backend`, which validates an enum-like string).
- The rejected-login error message is exactly `"Pilot mode active - admin only"` (spaces become `+` when redirected as a query string, matching the existing `/?error=...` pattern, e.g. `Admin+password+required`).
- The owner-required error modal message is exactly `"Enter who this song is for."`.
- `effective_username` (the resolved queue-owner name) must replace `username` in **both** call sites in `search.py`: the immediate `queue_manager.add_to_queue(...)` call in `queue_video()`, and the `background_tasks.add_task(download_video_and_queue, ...)` call (whose last positional arg is the username, per `download_video_and_queue`'s signature at `search.py:244-251`). Rate limiting and logging still key off the real session `username`, not the owner.
- `partials/search_results.html`'s owner `<input>` is only added when `is_admin` is true; regular (non-admin) result cards are byte-for-byte unchanged.
- Existing tests in `tests/test_routes.py` (e.g. `_queue_form()`, `test_queue_video_already_downloaded`, `test_search_happy_path`) must keep passing unmodified — none of them set `is_admin=True` or pass an `owner` field, so the new branches must be opt-in additions, not replacements of existing behavior.

---

### Task 1: `pilot_mode` config setting

**Files:**
- Modify: `app/config.py`
- Modify: `.env.example`
- Test: `tests/test_config_extra.py`

**Interfaces:**
- Produces: `Settings.pilot_mode: bool` (default `False`), readable as `settings.pilot_mode` from any module that imports `app.config.settings`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config_extra.py` (after the existing path-helper tests, using the file's `_make()` helper):

```python
def test_pilot_mode_defaults_false():
    """pilot_mode is off unless explicitly enabled."""
    settings = _make()
    assert settings.pilot_mode is False


def test_pilot_mode_env_var_true(monkeypatch):
    """PILOT_MODE=true enables pilot mode."""
    monkeypatch.setenv("PILOT_MODE", "true")
    settings = Settings(
        admin_password="a-real-password",
        youtube_api_key="a-real-key",
        secret_key=VALID_SECRET,
    )
    assert settings.pilot_mode is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config_extra.py -k pilot_mode -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'pilot_mode'`

- [ ] **Step 3: Add the setting**

In `app/config.py`, add to the `Settings` class right after `idle_video_path` (line 56):

```python
    # Single-admin-only operating mode: rejects non-admin login and exposes
    # an admin search-and-queue-for-others card. Off by default.
    pilot_mode: bool = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config_extra.py -k pilot_mode -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Document the env var**

In `.env.example`, add after the `IDLE_VIDEO_PATH` block (after line 67):

```
# Pilot mode (default: false). When true, only the admin account can log in
# - the admin searches and queues songs on behalf of whoever's turn it is,
# via a new card in the admin panel. Useful for parties run by one person
# instead of everyone using their own phone.
PILOT_MODE=false
```

- [ ] **Step 6: Run full config test suite**

Run: `uv run pytest tests/test_config_extra.py tests/test_config_validation.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add app/config.py .env.example tests/test_config_extra.py
git commit -m "feat: add PILOT_MODE config setting"
```

---

### Task 2: Reject non-admin login when pilot mode is on

**Files:**
- Modify: `app/routes/auth.py`
- Test: `tests/test_routes.py`

**Interfaces:**
- Consumes: `settings.pilot_mode` (Task 1).
- Produces: no new functions — `POST /login` gains an early-return branch.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_routes.py`, directly after `test_login_empty_username_redirects_with_error` (after line 114):

```python
def test_login_pilot_mode_rejects_non_admin(monkeypatch):
    """A non-admin login is rejected while pilot mode is active."""
    monkeypatch.setattr(auth_module.settings, "pilot_mode", True)
    response = client.post(
        "/login", data={"username": "carol"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "Pilot+mode+active" in response.headers["location"]


def test_login_pilot_mode_allows_admin(monkeypatch, _fresh_admin_limiter):
    """Admin login still succeeds while pilot mode is active."""
    monkeypatch.setattr(auth_module.settings, "pilot_mode", True)
    monkeypatch.setattr(auth_module.settings, "admin_password", "s3cret-pass")
    response = client.post(
        "/login",
        data={"username": "admin", "password": "s3cret-pass"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"


def test_login_pilot_mode_off_allows_non_admin():
    """Pilot mode off (the default) leaves normal login untouched."""
    response = client.post(
        "/login", data={"username": "carol"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/app"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_routes.py -k pilot_mode -v`
Expected: `test_login_pilot_mode_rejects_non_admin` FAILS (carol logs in successfully instead of being rejected); the other two pass already (no code changed yet, but confirm alongside).

- [ ] **Step 3: Add the gating check**

In `app/routes/auth.py`, in `login()`, immediately after the empty-username check (after line 159, before `is_admin = False` on line 161):

```python
    if settings.pilot_mode and username.lower() != "admin":
        return RedirectResponse(
            url="/?error=Pilot+mode+active+-+admin+only", status_code=303
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_routes.py -k "pilot_mode or login" -v`
Expected: All PASS (new pilot_mode tests + all pre-existing login tests)

- [ ] **Step 5: Commit**

```bash
git add app/routes/auth.py tests/test_routes.py
git commit -m "feat: reject non-admin login when pilot mode is active"
```

---

### Task 3: Admin page gets `pilot_mode` in its template context, and admin.html scaffolds the gated card

**Files:**
- Modify: `app/routes/admin.py`
- Modify: `app/templates/admin.html`
- Test: `tests/test_routes.py`

**Interfaces:**
- Consumes: `settings.pilot_mode` (Task 1).
- Produces: `admin.html` renders a card with `id="pilot-search-card"` containing a search form (`hx-post="/search"`, `hx-target="#pilot-search-results"`) and an empty results container `id="pilot-search-results"`, visible only when `pilot_mode` is true. This id is what Task 4/5's tests and templates target.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_routes.py`, directly after `test_admin_page_chromecast_omits_output_selection_controls` (after line 619):

```python
def test_admin_page_pilot_mode_shows_search_card(_admin_mocks, monkeypatch):
    """The pilot-mode search card renders when pilot_mode is on."""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "pilot_mode", True)
    html = _admin_client().get("/admin/").text
    assert 'id="pilot-search-card"' in html
    assert 'id="pilot-search-results"' in html


def test_admin_page_pilot_mode_off_hides_search_card(_admin_mocks, monkeypatch):
    """The pilot-mode search card is absent when pilot_mode is off (default)."""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "pilot_mode", False)
    html = _admin_client().get("/admin/").text
    assert 'id="pilot-search-card"' not in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_routes.py -k pilot_mode_shows_search_card -v`
Expected: FAIL (`pilot-search-card` not found — the template doesn't have it yet; also `pilot_mode` isn't in the render context yet)

- [ ] **Step 3: Pass `pilot_mode` into the admin page context**

In `app/routes/admin.py`, in `admin_page()`, add `"pilot_mode": settings.pilot_mode` to the dict returned by `templates.TemplateResponse` (alongside the existing `"player_backend"` key, around line 47):

```python
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "username": username,
            "is_admin": is_admin,
            "queue": queue,
            "player_backend": settings.player_backend,
            "pilot_mode": settings.pilot_mode,
        },
    )
```

- [ ] **Step 4: Add the gated card to admin.html**

In `app/templates/admin.html`, insert this new section between the closing `</div>` of the Queue Section (line 41) and the opening `<!-- Playout Control Section -->` comment (line 43):

```html
<!-- Pilot Mode: Queue a Song Section -->
{% if pilot_mode %}
<div class="mb-6" id="pilot-search-card">
    <h2 class="text-2xl font-bold mb-4">Queue a Song</h2>

    <div class="card bg-base-100 shadow-xl">
        <div class="card-body">
            <!-- Search Form -->
            <form
                hx-post="/search"
                hx-target="#pilot-search-results"
                hx-swap="innerHTML"
            >
                <div class="form-control">
                    <div class="flex gap-2 w-full">
                        <input
                            type="text"
                            name="query"
                            placeholder="Search for a song or artist..."
                            class="input input-bordered flex-1"
                            required
                        />
                        <button type="submit" class="btn btn-primary">
                            Search
                        </button>
                    </div>
                    <label class="label">
                        <span class="label-text-alt">💡 Tip: We'll automatically search for karaoke versions!</span>
                    </label>
                </div>
            </form>

            <!-- Search Results Container -->
            <div id="pilot-search-results" class="mt-4">
                <!-- Results will be loaded here via HTMX -->
            </div>
        </div>
    </div>
</div>
{% endif %}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_routes.py -k "pilot_mode or admin_page" -v`
Expected: All PASS

- [ ] **Step 6: Run full route test suite (regression check)**

Run: `uv run pytest tests/test_routes.py -v`
Expected: All PASS (no pre-existing test touches `pilot_mode` or `#pilot-search-card`, so nothing should break)

- [ ] **Step 7: Commit**

```bash
git add app/routes/admin.py app/templates/admin.html tests/test_routes.py
git commit -m "feat: add pilot-mode search card scaffold to admin panel"
```

---

### Task 4: Owner input on search results for admin callers

**Files:**
- Modify: `app/routes/search.py`
- Modify: `app/templates/partials/search_results.html`
- Test: `tests/test_routes.py`

**Interfaces:**
- Consumes: `is_admin` (already returned by `get_session_user(request)` in the `/search` route, per `search.py:75`).
- Produces: `partials/search_results.html` accepts an `is_admin` boolean in its render context; each result card's form gains `<input type="text" name="owner" placeholder="Who's this for?" required>` only when `is_admin` is true. This is a template-only, additive change — the hidden `title`/`thumbnail_url`/`duration`/`views` inputs are unchanged.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_routes.py`, directly after `test_search_happy_path` (after line 265):

```python
def test_search_happy_path_admin_shows_owner_field(monkeypatch):
    """Admin search results include a per-card owner input."""
    video = SimpleNamespace(
        video_id=VALID_VIDEO_ID,
        title="Bohemian Rhapsody Karaoke",
        thumbnail_url="http://img/thumb.jpg",
        duration=183,
        views=1000,
    )
    monkeypatch.setattr(
        search_module.youtube_service, "search", AsyncMock(return_value=[video])
    )
    c = _session_client("admin", True)
    response = c.post("/search", data={"query": "queen"})
    assert response.status_code == 200
    assert 'name="owner"' in response.text


def test_search_happy_path_non_admin_omits_owner_field(monkeypatch):
    """Non-admin search results do not include an owner input."""
    video = SimpleNamespace(
        video_id=VALID_VIDEO_ID,
        title="Bohemian Rhapsody Karaoke",
        thumbnail_url="http://img/thumb.jpg",
        duration=183,
        views=1000,
    )
    monkeypatch.setattr(
        search_module.youtube_service, "search", AsyncMock(return_value=[video])
    )
    c = _session_client("alice", False)
    response = c.post("/search", data={"query": "queen"})
    assert response.status_code == 200
    assert 'name="owner"' not in response.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_routes.py -k owner_field -v`
Expected: `test_search_happy_path_admin_shows_owner_field` FAILS (no `owner` input rendered yet)

- [ ] **Step 3: Pass `is_admin` into the search results context**

In `app/routes/search.py`, in `search()`'s success branch (around line 102-110), add `"is_admin": is_admin` to the context dict:

```python
        return templates.TemplateResponse(
            request,
            "partials/search_results.html",
            {
                "results": results,
                "username": username,
                "is_admin": is_admin,
                "error": None,
            },
        )
```

- [ ] **Step 4: Add the conditional owner input to the template**

In `app/templates/partials/search_results.html`, inside the per-card `<form>` (lines 52-64), add the owner input right after the hidden `views` field (after line 60), before the submit button:

```html
                    <form
                        hx-post="/queue/{{ video.video_id }}"
                        hx-target="#modal-container"
                        hx-swap="innerHTML"
                    >
                        <input type="hidden" name="title" value="{{ video.title }}">
                        <input type="hidden" name="thumbnail_url" value="{{ video.thumbnail_url }}">
                        <input type="hidden" name="duration" value="{{ video.duration }}">
                        <input type="hidden" name="views" value="{{ video.views }}">
                        {% if is_admin %}
                        <input
                            type="text"
                            name="owner"
                            placeholder="Who's this for?"
                            class="input input-bordered input-sm w-32"
                            required
                        >
                        {% endif %}
                        <button type="submit" class="btn btn-primary btn-sm">
                            + Queue
                        </button>
                    </form>
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_routes.py -k "owner_field or search" -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add app/routes/search.py app/templates/partials/search_results.html tests/test_routes.py
git commit -m "feat: show per-song owner field on admin search results"
```

---

### Task 5: Queue endpoint resolves owner name for admin callers

**Files:**
- Modify: `app/routes/search.py`
- Test: `tests/test_routes.py`

**Interfaces:**
- Consumes: `owner: str = Form("")` (new optional form field on `POST /queue/{video_id}`).
- Produces: when `is_admin` is true, the video is queued under the (required, non-blank) `owner` name instead of the session `username`; non-admin behavior is unchanged.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_routes.py`, directly after `test_queue_video_already_downloaded` (after line 340):

```python
def _admin_queue_form(owner: str = "Bob") -> dict:
    """Return valid form fields for an admin POST /queue/{id} request."""
    form = _queue_form()
    form["owner"] = owner
    return form


def test_queue_video_admin_uses_owner_name(monkeypatch):
    """An admin queueing a song attributes it to the typed owner, not 'admin'."""
    monkeypatch.setattr(search_module._queue_limiter, "allow", lambda key: True)
    monkeypatch.setattr(
        search_module.download_service, "is_downloaded", Mock(return_value=True)
    )
    add = AsyncMock()
    monkeypatch.setattr(search_module.queue_manager, "add_to_queue", add)

    c = _session_client("admin", True)
    response = c.post(f"/queue/{VALID_VIDEO_ID}", data=_admin_queue_form("Bob"))

    assert response.status_code == 200
    assert "added to queue" in response.text
    add.assert_awaited_once()
    assert add.call_args.kwargs["username"] == "Bob"


def test_queue_video_admin_missing_owner_rejected(monkeypatch):
    """An admin queueing without an owner name gets a validation error, not a fallback."""
    monkeypatch.setattr(search_module._queue_limiter, "allow", lambda key: True)
    monkeypatch.setattr(
        search_module.download_service, "is_downloaded", Mock(return_value=True)
    )
    add = AsyncMock()
    monkeypatch.setattr(search_module.queue_manager, "add_to_queue", add)

    c = _session_client("admin", True)
    form = _queue_form()  # no "owner" key at all
    response = c.post(f"/queue/{VALID_VIDEO_ID}", data=form)

    assert response.status_code == 200
    assert "Enter who this song is for." in response.text
    add.assert_not_awaited()


def test_queue_video_non_admin_owner_field_ignored(monkeypatch):
    """A non-admin session cannot use the owner field to queue as someone else."""
    monkeypatch.setattr(search_module._queue_limiter, "allow", lambda key: True)
    monkeypatch.setattr(
        search_module.download_service, "is_downloaded", Mock(return_value=True)
    )
    add = AsyncMock()
    monkeypatch.setattr(search_module.queue_manager, "add_to_queue", add)

    c = _session_client("alice", False)
    response = c.post(f"/queue/{VALID_VIDEO_ID}", data=_admin_queue_form("Bob"))

    assert response.status_code == 200
    add.assert_awaited_once()
    assert add.call_args.kwargs["username"] == "alice"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_routes.py -k queue_video_admin -v`
Expected: FAIL — `owner` isn't accepted as a form field yet (FastAPI ignores unknown form fields, so `add_to_queue` is still called with `username="admin"`, not `"Bob"`; the missing-owner test fails because there's no rejection).

- [ ] **Step 3: Add the `owner` field and `effective_username` resolution**

In `app/routes/search.py`, add `owner: str = Form("")` to `queue_video()`'s signature (after `views: int = Form(...)`, around line 138):

```python
async def queue_video(
    request: Request,
    video_id: str,
    background_tasks: BackgroundTasks,
    title: str = Form(...),
    thumbnail_url: str = Form(...),
    duration: int = Form(...),
    views: int = Form(...),
    owner: str = Form(""),
):
```

Then, right after `username, is_admin = get_session_user(request)` (line 146), add the resolution block:

```python
    effective_username = username
    if is_admin:
        owner = owner.strip()
        if not owner:
            return templates.TemplateResponse(
                request,
                "partials/modals.html",
                {
                    "modal_type": "error",
                    "message": "Enter who this song is for.",
                },
            )
        effective_username = owner
```

Finally, replace `username` with `effective_username` in the two places it flows to the queue (leave every other `username` reference — rate limiting, logging — untouched):

- In the background-download branch (around line 184-192), the `background_tasks.add_task(...)` call's last argument changes from `username` to `effective_username`.
- In the immediate-add branch (around line 204-211), `queue_manager.add_to_queue(...)`'s `username=username` becomes `username=effective_username`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_routes.py -k "queue_video" -v`
Expected: All PASS, including every pre-existing `test_queue_video_*` test (they never pass `is_admin=True`, so `effective_username` stays equal to `username` for them)

- [ ] **Step 5: Run the full test suite (regression check)**

Run: `uv run pytest`
Expected: All PASS

- [ ] **Step 6: Lint and format check**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: Clean

- [ ] **Step 7: Commit**

```bash
git add app/routes/search.py tests/test_routes.py
git commit -m "feat: let admin queue songs under a typed owner name"
```

---

### Task 6: Documentation

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md` (if it documents environment variables — check for an existing table/list near `PLAYER_BACKEND`/`IDLE_VIDEO_PATH`)

**Interfaces:**
- None (documentation only).

- [ ] **Step 1: Add `PILOT_MODE` to CLAUDE.md's environment variable list**

In `/Users/graeme/Code/karaoke-jukebox/CLAUDE.md`, in the "Required environment variables" section (which already lists `PLAYER_BACKEND` and `IDLE_VIDEO_PATH`), add:

```
- `PILOT_MODE` - Single-admin-only mode (default: false). When true, only the
  admin account can log in; the admin panel gains a search-and-queue card for
  queueing songs on behalf of whoever's turn it is.
```

- [ ] **Step 2: Check README.md for an environment variable section**

Run: `grep -n "PLAYER_BACKEND\|Environment Variable" README.md`

If README.md documents environment variables in a list/table (it references `PLAYER_BACKEND`/`IDLE_VIDEO_PATH` around line 187-198 per the existing mpv section), add a short paragraph there describing `PILOT_MODE` the same way `PLAYER_BACKEND` is documented — one sentence on what it does, one on the admin-panel card it adds. If README.md has no such section, skip this step (CLAUDE.md is sufficient).

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: document PILOT_MODE setting"
```

---

## Final Verification

After all tasks are complete:

- [ ] Run: `uv run pytest -v` — all tests pass
- [ ] Run: `uv run ruff check . && uv run ruff format --check .` — clean
- [ ] Manually verify (or note as a follow-up if no live environment is available): set `PILOT_MODE=true`, confirm a non-admin login is rejected, log in as admin, confirm the "Queue a Song" card appears, search for a song, confirm the owner field appears on each result card, queue one with an owner name, confirm it appears in the admin queue view attributed to that name and not "admin".
