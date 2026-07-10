# Pilot Mode (Admin-Only Queueing) — Design

Date: 2026-07-10
Status: Approved, pending implementation

## Context

Karaoke Jukebox normally has multiple people logging in on their own phones,
searching, and queueing their own songs (`app/routes/search.py`, `app/templates/app.html`).
"Pilot mode" is an alternate operating mode for parties where one person (the
admin, running the laptop/Pi) drives the whole session: they search on behalf
of whoever wants to sing next and record whose song it is, instead of handing
out logins. The queue itself is unaffected — it's still `(song, owner)` rows
(`app/services/queue_manager.py:add_to_queue`, which already takes an arbitrary
`username` string) — only *who is allowed to add rows, and how* changes.

## Decisions

1. **Toggled by a new `PILOT_MODE` env var** (`app/config.py`), a plain
   `bool` setting defaulting to `False`. Matches the existing `PLAYER_BACKEND`
   pattern: a startup-time flag, no runtime toggle, no persistence beyond
   restart.
2. **When `PILOT_MODE=true`, non-admin login is rejected.** `POST /login`
   returns to the login page with an error (`"Pilot mode active - admin
   only"`) for any username other than `admin`. The regular login/`/app` flow
   is not otherwise modified — it simply becomes unreachable while pilot mode
   is on, since no non-admin session can ever be created.
3. **The admin gets a new "Queue a Song" search card**, shown in `admin.html`
   only when `pilot_mode` is true, placed between the existing Queue Section
   and the Playout Control Section. It reuses the existing `/search` and
   `POST /queue/{video_id}` endpoints and the existing
   `partials/search_form.html` / `partials/search_results.html` partials
   rather than introducing parallel admin-only routes — search/queue logic
   isn't duplicated, only extended.
4. **Owner is a per-song field, not a persistent "current singer".** Each
   result card in the admin's search results gets its own visible "who's this
   for?" text input (in addition to the existing hidden `title` /
   `thumbnail_url` / `duration` / `views` fields already on that form). The
   admin types a name before clicking "Add to Queue" on that specific card;
   nothing is remembered between songs.
5. **The card's visibility is the only gate** — it isn't a separately
   toggleable feature. An admin who wants to queue on someone else's behalf
   outside of pilot mode does not get this card; the two concerns
   (login-gating, admin-queues-for-others UI) are both driven by the single
   `pilot_mode` flag.

## Design

### Config (`app/config.py`)

```python
pilot_mode: bool = False
```

No custom validator needed — pydantic-settings parses `PILOT_MODE=true/false`
natively for bool fields. Document it in `load_settings()`'s "Optional
environment variables" log block alongside `PLAYER_BACKEND`.

### Login gating (`app/routes/auth.py`)

In `login()`, immediately after the existing empty-username check
(currently around line 158-159), add:

```python
if settings.pilot_mode and username.lower() != "admin":
    return RedirectResponse(
        url="/?error=Pilot+mode+active+-+admin+only", status_code=303
    )
```

The existing admin password/rate-limit path below is unchanged.

### Admin route context (`app/routes/admin.py`)

The `/admin` route's template context gains `"pilot_mode": settings.pilot_mode`
so `admin.html` can gate the new card.

### Admin template (`app/templates/admin.html`)

New card, `{% if pilot_mode %}` gated, inserted between the Queue Section and
the Playout Control Section:

- A search form matching `app.html`'s (posts to `/search`, HTMX swap into its
  own results container — a distinct element id from `app.html`'s
  `#search-results` so the two never collide if both templates were ever
  rendered in the same session, though in practice only one page applies to
  any given login).
- Results render via `partials/search_results.html` as today.

### Search results partial (`app/templates/partials/search_results.html`)

Needs `is_admin` in its render context (currently `POST /search` only passes
`results`/`username`/`error` — `is_admin` must be added there too). When
`is_admin` is true, each result card's form gains a visible:

```html
<input type="text" name="owner" placeholder="Who's this for?" required>
```

Regular (non-admin) result cards are unchanged.

### Queue route (`app/routes/search.py`)

`POST /queue/{video_id}` gains `owner: str = Form("")`. Right after the
existing `username, is_admin = get_session_user(request)` line:

```python
effective_username = username
if is_admin:
    owner = owner.strip()
    if not owner:
        return templates.TemplateResponse(
            request,
            "partials/modals.html",
            {"modal_type": "error", "message": "Enter who this song is for."},
        )
    effective_username = owner
```

`effective_username` replaces `username` in both the immediate-add branch
(`queue_manager.add_to_queue(..., username=effective_username)`) and the
background-download branch (`download_video_and_queue(..., effective_username)`
and its downstream `add_to_queue` call). `username` (the logged-in admin) is
still used for rate-limiting (`_rate_limit_key`) and logging, so multiple
songs entered by the same admin session share one rate-limit bucket
regardless of owner name — this matches today's per-*session* throttling
intent (guarding download/disk abuse from one browser tab), not per-owner
throttling.

No changes to `queue_manager.add_to_queue`: it already accepts an arbitrary
`username` string, so an owner name that isn't a real login "just works",
including the existing duplicate-check (same owner + same video_id already
queued is rejected, same as today) and `admin_queue.html`'s existing display
of the `username` column.

## Out of scope

- Persisting `pilot_mode` or the owner name across restarts/songs.
- A runtime admin toggle for pilot mode (env-var only, like `PLAYER_BACKEND`).
- Validating owner names against any format (mirrors the existing login
  username, which only requires non-empty after `.strip()`).
- Any change to `/app`, `app.html`, or the regular login form beyond the
  early rejection in `login()`.
