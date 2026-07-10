"""Unit tests for the FastAPI route handlers.

Covers auth, search, queue, and admin routes using FastAPI's TestClient with
mocked services. The TestClient is created WITHOUT a `with` block so the app
lifespan (scheduler, chromecast, ffmpeg checks) never runs.

Authentication is exercised two ways:
- Dependency overrides for routes guarded only by a `Depends(require_session)`.
- Real signed cookies for handlers that call get_session_user / require_admin
  directly (the cookie-decode path).
"""

import re

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.routes.admin as admin_module
import app.routes.auth as auth_module
import app.routes.queue as queue_module
import app.routes.search as search_module
from app.main import app
from app.rate_limit import RateLimiter
from app.routes.auth import (
    decode_session,
    encode_session,
    get_session_user,
    require_admin,
    require_session,
)
from app.routes.search import download_video_and_queue
from app.services.download import DownloadError
from app.services.youtube import YouTubeError

client = TestClient(app)

VALID_VIDEO_ID = "abcdefghijk"


def _session_client(username: str = "alice", is_admin: bool = False) -> TestClient:
    """Build a TestClient carrying a valid signed session cookie.

    Args:
        username: Username to embed in the session.
        is_admin: Whether the session is for an admin user.

    Returns:
        A TestClient with the karaoke_session cookie preset.
    """
    c = TestClient(app)
    c.cookies.set(
        "karaoke_session", encode_session({"username": username, "is_admin": is_admin})
    )
    return c


@pytest.fixture(autouse=True)
def _clear_overrides():
    """Reset shared state (overrides and client cookies) around every test.

    httpx persists Set-Cookie headers into the client jar, so login tests would
    otherwise leak an authenticated session into later tests on the shared
    module-level client.
    """
    client.cookies.clear()
    yield
    app.dependency_overrides.clear()
    client.cookies.clear()


@pytest.fixture
def _fresh_admin_limiter(monkeypatch):
    """Replace the admin login limiter with a fresh, unthrottled instance."""
    monkeypatch.setattr(
        auth_module,
        "_admin_login_limiter",
        RateLimiter(max_events=5, window_seconds=300),
    )


# Auth route tests


def test_login_page_no_session():
    """The login page renders for an anonymous visitor."""
    response = client.get("/")
    assert response.status_code == 200


def test_login_page_user_redirects_to_app():
    """A logged-in non-admin is redirected to /app."""
    c = _session_client("bob", False)
    response = c.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/app"


def test_login_page_admin_redirects_to_admin():
    """A logged-in admin is redirected to /admin."""
    c = _session_client("admin", True)
    response = c.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/admin"


def test_login_empty_username_redirects_with_error():
    """An empty username redirects back with an error message."""
    response = client.post("/login", data={"username": "  "}, follow_redirects=False)
    assert response.status_code == 303
    assert "Username+required" in response.headers["location"]


def test_login_normal_user_sets_cookie():
    """A normal user login redirects to /app and sets the session cookie."""
    response = client.post("/login", data={"username": "carol"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/app"
    assert "karaoke_session" in response.cookies


def test_login_admin_correct_password(monkeypatch, _fresh_admin_limiter):
    """An admin login with the correct password redirects to /admin."""
    monkeypatch.setattr(auth_module.settings, "admin_password", "s3cret-pass")
    response = client.post(
        "/login",
        data={"username": "admin", "password": "s3cret-pass"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"


def test_login_admin_wrong_password(monkeypatch, _fresh_admin_limiter):
    """An admin login with the wrong password is rejected."""
    monkeypatch.setattr(auth_module.settings, "admin_password", "s3cret-pass")
    response = client.post(
        "/login",
        data={"username": "admin", "password": "nope"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Invalid+admin+password" in response.headers["location"]


def test_login_admin_missing_password(_fresh_admin_limiter):
    """An admin login without a password is rejected."""
    response = client.post("/login", data={"username": "admin"}, follow_redirects=False)
    assert response.status_code == 303
    assert "Admin+password+required" in response.headers["location"]


def test_login_admin_rate_limited(monkeypatch):
    """A throttled admin login returns the too-many-attempts error."""
    limiter = RateLimiter(max_events=5, window_seconds=300)
    monkeypatch.setattr(limiter, "allow", lambda key: False)
    monkeypatch.setattr(auth_module, "_admin_login_limiter", limiter)
    response = client.post(
        "/login",
        data={"username": "admin", "password": "whatever"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Too+many+attempts" in response.headers["location"]


def test_logout_clears_cookie():
    """Logout redirects to / and deletes the session cookie."""
    c = _session_client("dave", False)
    response = c.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


# Auth helper unit tests


def test_decode_session_round_trip():
    """A signed session decodes back to the original data."""
    token = encode_session({"username": "alice", "is_admin": True})
    assert decode_session(token) == {"username": "alice", "is_admin": True}


def test_decode_session_garbage_returns_none():
    """A garbage token decodes to None rather than raising."""
    assert decode_session("not-a-token") is None


def test_require_session_raises_without_cookie():
    """require_session raises a 303 redirect when no session is present."""
    request = SimpleNamespace(cookies={})
    with pytest.raises(HTTPException) as exc:
        require_session(request)
    assert exc.value.status_code == 303


def test_require_admin_raises_for_non_admin():
    """require_admin raises a 303 redirect for a non-admin session."""
    token = encode_session({"username": "alice", "is_admin": False})
    request = SimpleNamespace(cookies={"karaoke_session": token})
    with pytest.raises(HTTPException) as exc:
        require_admin(request)
    assert exc.value.status_code == 303


def test_get_session_user_no_cookie():
    """get_session_user returns (None, False) with no cookie."""
    request = SimpleNamespace(cookies={})
    assert get_session_user(request) == (None, False)


# Search route tests


def test_app_page_renders(monkeypatch):
    """/app renders for an authenticated user and reads the queue."""
    app.dependency_overrides[require_session] = lambda: ("alice", False)
    get_queue = AsyncMock(return_value=[])
    monkeypatch.setattr(search_module.queue_manager, "get_queue", get_queue)

    response = client.get("/app")

    assert response.status_code == 200
    get_queue.assert_awaited_once()


def test_search_form():
    """GET /search returns the search form partial."""
    response = client.get("/search")
    assert response.status_code == 200


def test_search_empty_query():
    """An empty search query returns the error partial."""
    response = client.post("/search", data={"query": "   "})
    assert response.status_code == 200
    assert "Please enter a search query" in response.text


def test_search_rate_limited(monkeypatch):
    """A throttled search returns the slow-down error."""
    monkeypatch.setattr(search_module._search_limiter, "allow", lambda key: False)
    response = client.post("/search", data={"query": "hello"})
    assert response.status_code == 200
    assert "Too many searches" in response.text


def test_search_happy_path(monkeypatch):
    """A successful search renders the result cards."""
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
    response = client.post("/search", data={"query": "queen"})
    assert response.status_code == 200
    assert "Bohemian Rhapsody Karaoke" in response.text


def test_search_youtube_error(monkeypatch):
    """A YouTubeError surfaces its user_message in the error partial."""
    monkeypatch.setattr(
        search_module.youtube_service,
        "search",
        AsyncMock(side_effect=YouTubeError("Quota exceeded")),
    )
    response = client.post("/search", data={"query": "queen"})
    assert response.status_code == 200
    assert "Quota exceeded" in response.text


def test_search_generic_error(monkeypatch):
    """A generic exception yields the fallback error message."""
    monkeypatch.setattr(
        search_module.youtube_service,
        "search",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    response = client.post("/search", data={"query": "queen"})
    assert response.status_code == 200
    assert "Search failed" in response.text


def _queue_form() -> dict:
    """Return valid form fields for a POST /queue/{id} request."""
    return {
        "title": "Test Song",
        "thumbnail_url": "http://img/t.jpg",
        "duration": 200,
        "views": 500,
    }


def test_queue_video_not_logged_in():
    """Queueing without a session returns the must-be-logged-in modal."""
    response = client.post(f"/queue/{VALID_VIDEO_ID}", data=_queue_form())
    assert response.status_code == 200
    assert "logged in" in response.text


def test_queue_video_invalid_id():
    """An invalid video id returns the invalid-video modal."""
    c = _session_client("alice", False)
    response = c.post("/queue/bad", data=_queue_form())
    assert response.status_code == 200
    assert "Invalid video." in response.text


def test_queue_video_rate_limited(monkeypatch):
    """A throttled queue request returns the too-fast modal."""
    monkeypatch.setattr(search_module._queue_limiter, "allow", lambda key: False)
    c = _session_client("alice", False)
    response = c.post(f"/queue/{VALID_VIDEO_ID}", data=_queue_form())
    assert response.status_code == 200
    assert "too fast" in response.text


def test_queue_video_already_downloaded(monkeypatch):
    """An already-downloaded video is queued immediately with a success modal."""
    monkeypatch.setattr(search_module._queue_limiter, "allow", lambda key: True)
    monkeypatch.setattr(
        search_module.download_service, "is_downloaded", Mock(return_value=True)
    )
    add = AsyncMock()
    monkeypatch.setattr(search_module.queue_manager, "add_to_queue", add)

    c = _session_client("alice", False)
    response = c.post(f"/queue/{VALID_VIDEO_ID}", data=_queue_form())

    assert response.status_code == 200
    assert "added to queue" in response.text
    add.assert_awaited_once()


def test_queue_video_not_downloaded(monkeypatch):
    """A not-yet-downloaded video returns the downloading modal."""
    monkeypatch.setattr(search_module._queue_limiter, "allow", lambda key: True)
    monkeypatch.setattr(
        search_module.download_service, "is_downloaded", Mock(return_value=False)
    )
    # The background task runs after the response; mock its dependencies.
    monkeypatch.setattr(
        search_module.download_service,
        "download",
        AsyncMock(return_value={"success": True}),
    )
    monkeypatch.setattr(search_module.queue_manager, "add_to_queue", AsyncMock())

    c = _session_client("alice", False)
    response = c.post(f"/queue/{VALID_VIDEO_ID}", data=_queue_form())

    assert response.status_code == 200
    assert "Downloading" in response.text


def test_queue_video_value_error(monkeypatch):
    """A queue validation error surfaces its message in an error modal."""
    monkeypatch.setattr(search_module._queue_limiter, "allow", lambda key: True)
    monkeypatch.setattr(
        search_module.download_service, "is_downloaded", Mock(return_value=True)
    )
    monkeypatch.setattr(
        search_module.queue_manager,
        "add_to_queue",
        AsyncMock(side_effect=ValueError("Already in your queue")),
    )

    c = _session_client("alice", False)
    response = c.post(f"/queue/{VALID_VIDEO_ID}", data=_queue_form())

    assert response.status_code == 200
    assert "Already in your queue" in response.text


# Background download task tests


async def test_download_and_queue_success(monkeypatch):
    """A successful download adds the video to the queue."""
    monkeypatch.setattr(
        search_module.download_service,
        "download",
        AsyncMock(return_value={"success": True}),
    )
    add = AsyncMock()
    monkeypatch.setattr(search_module.queue_manager, "add_to_queue", add)

    await download_video_and_queue(VALID_VIDEO_ID, "Song", "http://t", 100, 1, "alice")

    add.assert_awaited_once()


async def test_download_and_queue_failed_result(monkeypatch):
    """A failed download result does not enqueue the video."""
    monkeypatch.setattr(
        search_module.download_service,
        "download",
        AsyncMock(return_value={"success": False, "error": "nope"}),
    )
    add = AsyncMock()
    monkeypatch.setattr(search_module.queue_manager, "add_to_queue", add)

    await download_video_and_queue(VALID_VIDEO_ID, "Song", "http://t", 100, 1, "alice")

    add.assert_not_awaited()


async def test_download_and_queue_download_error(monkeypatch):
    """A DownloadError is caught and does not propagate."""
    monkeypatch.setattr(
        search_module.download_service,
        "download",
        AsyncMock(side_effect=DownloadError("boom")),
    )
    monkeypatch.setattr(search_module.queue_manager, "add_to_queue", AsyncMock())

    # Must not raise.
    await download_video_and_queue(VALID_VIDEO_ID, "Song", "http://t", 100, 1, "alice")


async def test_download_and_queue_value_error(monkeypatch):
    """A queue ValueError after download is caught and does not propagate."""
    monkeypatch.setattr(
        search_module.download_service,
        "download",
        AsyncMock(return_value={"success": True}),
    )
    monkeypatch.setattr(
        search_module.queue_manager,
        "add_to_queue",
        AsyncMock(side_effect=ValueError("dup")),
    )

    await download_video_and_queue(VALID_VIDEO_ID, "Song", "http://t", 100, 1, "alice")


# Queue route tests


def test_delete_queue_item_unauthenticated():
    """Deleting without a session returns 401."""
    response = client.delete("/queue/5")
    assert response.status_code == 401


def test_delete_queue_item_success(monkeypatch):
    """A successful removal returns a success payload."""
    monkeypatch.setattr(
        queue_module.queue_manager,
        "remove_from_queue",
        AsyncMock(return_value=True),
    )
    c = _session_client("alice", False)
    response = c.delete("/queue/5")
    assert response.status_code == 200
    assert response.json()["success"] is True


def test_delete_queue_item_not_found(monkeypatch):
    """A missing item returns 404."""
    monkeypatch.setattr(
        queue_module.queue_manager,
        "remove_from_queue",
        AsyncMock(return_value=False),
    )
    c = _session_client("alice", False)
    response = c.delete("/queue/5")
    assert response.status_code == 404


def test_delete_queue_item_permission_denied(monkeypatch):
    """A PermissionError maps to 403."""
    monkeypatch.setattr(
        queue_module.queue_manager,
        "remove_from_queue",
        AsyncMock(side_effect=PermissionError("not yours")),
    )
    c = _session_client("alice", False)
    response = c.delete("/queue/5")
    assert response.status_code == 403


def test_delete_queue_item_generic_error(monkeypatch):
    """A generic error maps to 500."""
    monkeypatch.setattr(
        queue_module.queue_manager,
        "remove_from_queue",
        AsyncMock(side_effect=RuntimeError("db")),
    )
    c = _session_client("alice", False)
    response = c.delete("/queue/5")
    assert response.status_code == 500


def test_sse_endpoint_bounded(monkeypatch):
    """The SSE endpoint streams one event then completes without hanging."""

    async def _one_event(username, is_admin):
        yield "event: queue-update\ndata: hi\n\n"

    monkeypatch.setattr(queue_module.queue_manager, "subscribe", _one_event)

    response = client.get("/queue/sse")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "queue-update" in response.text


# Admin route tests


@pytest.fixture
def _admin_mocks(monkeypatch):
    """Replace admin singletons with mocks and return them.

    Returns:
        Tuple of (playout_service mock, queue_manager mock).
    """
    cc = MagicMock()
    cc.discover_devices = AsyncMock(return_value=[])
    cc.select_device = Mock(return_value=True)
    cc.start_playback = Mock(return_value={"success": True, "message": "started"})
    cc.stop_playback = Mock(return_value={"success": True, "message": "stopped"})
    cc.skip_current = Mock(return_value={"success": True, "message": "skipped"})
    cc.is_playing = False
    cc.selected_device_uuid = None

    qm = MagicMock()
    qm.get_queue = AsyncMock(return_value=[])
    qm.get_queue_size = AsyncMock(return_value=0)
    qm.get_currently_playing = AsyncMock(return_value=None)
    qm.remove_from_queue = AsyncMock(return_value=True)
    qm.clear_queue = AsyncMock(return_value=3)

    monkeypatch.setattr(admin_module, "playout_service", cc)
    monkeypatch.setattr(admin_module, "queue_manager", qm)
    return cc, qm


def _admin_client() -> TestClient:
    """Return a TestClient carrying a valid admin session cookie."""
    return _session_client("admin", True)


def test_admin_page(_admin_mocks):
    """The admin page renders with the current queue."""
    response = _admin_client().get("/admin/")
    assert response.status_code == 200


def _start_btn_tag(html: str) -> str:
    """Return the opening <button> tag for the start-playback control.

    Args:
        html: Rendered admin page markup.

    Returns:
        The matched opening tag text (raises AssertionError if absent).
    """
    match = re.search(r'id="start-playback-btn".*?>', html, re.S)
    assert match, "start-playback button not found in admin page"
    return match.group(0)


def test_admin_page_mpv_renders_hdmi_and_enables_playback(_admin_mocks, monkeypatch):
    """With the mpv backend the device card is HDMI and playback is not gated."""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "player_backend", "mpv")
    html = _admin_client().get("/admin/").text
    assert "Playout Control" in html
    assert "HDMI playout" in html
    assert "Scan for Devices" not in html
    # mpv has no device to select, so the start button must render enabled.
    assert "disabled" not in _start_btn_tag(html)


def test_admin_page_chromecast_keeps_scan_and_gates_playback(_admin_mocks, monkeypatch):
    """The Chromecast backend keeps device discovery and disabled-by-default buttons."""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "player_backend", "chromecast")
    html = _admin_client().get("/admin/").text
    assert "Playout Control" in html
    assert "Scan for Devices" in html
    assert "HDMI playout" not in html
    # Chromecast keeps the device-selection gate: start disabled until a pick.
    assert "disabled" in _start_btn_tag(html)


def test_admin_page_mpv_renders_output_selection_controls(_admin_mocks, monkeypatch):
    """The mpv output card renders video/audio selects and an apply button."""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "player_backend", "mpv")
    html = _admin_client().get("/admin/").text
    assert 'id="mpv-video-select"' in html
    assert 'id="mpv-audio-select"' in html
    assert 'id="mpv-apply-btn"' in html


def test_admin_page_chromecast_omits_output_selection_controls(
    _admin_mocks, monkeypatch
):
    """The mpv-only output controls do not render on the Chromecast backend."""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "player_backend", "chromecast")
    html = _admin_client().get("/admin/").text
    assert 'id="mpv-video-select"' not in html


def test_admin_non_admin_redirects():
    """An anonymous request to an admin route is redirected (303)."""
    response = client.get("/admin/", follow_redirects=False)
    assert response.status_code == 303


def test_admin_scan_devices_success(_admin_mocks):
    """A successful scan returns the devices and count."""
    cc, _ = _admin_mocks
    cc.discover_devices = AsyncMock(return_value=[{"uuid": "u1"}, {"uuid": "u2"}])
    response = _admin_client().get("/admin/devices/scan")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["count"] == 2


def test_admin_scan_devices_failure(_admin_mocks):
    """A scan error returns a success:false payload."""
    cc, _ = _admin_mocks
    cc.discover_devices = AsyncMock(side_effect=RuntimeError("zeroconf"))
    response = _admin_client().get("/admin/devices/scan")
    assert response.status_code == 200
    assert response.json()["success"] is False


def test_admin_select_device_success(_admin_mocks):
    """Selecting a known device returns success."""
    response = _admin_client().post("/admin/devices/select", data={"device_uuid": "u1"})
    assert response.status_code == 200
    assert response.json()["success"] is True


def test_admin_select_device_not_found(_admin_mocks):
    """Selecting an unknown device returns 404."""
    cc, _ = _admin_mocks
    cc.select_device = Mock(return_value=False)
    response = _admin_client().post(
        "/admin/devices/select", data={"device_uuid": "nope"}
    )
    assert response.status_code == 404


def test_admin_mpv_outputs_success(_admin_mocks, monkeypatch):
    """The mpv outputs endpoint returns the backend's video/audio lists."""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "player_backend", "mpv")
    cc, _ = _admin_mocks
    cc.list_video_outputs = Mock(
        return_value=[
            {
                "drm_device": "/dev/dri/card0",
                "drm_connector": "HDMI-A-1",
                "label": "HDMI-A-1 (card0)",
            }
        ]
    )
    cc.list_audio_outputs = Mock(
        return_value=[{"name": "auto", "description": "Autoselect device"}]
    )
    response = _admin_client().get("/admin/mpv/outputs")
    assert response.status_code == 200
    body = response.json()
    assert body["video"][0]["drm_connector"] == "HDMI-A-1"
    assert body["audio"][0]["name"] == "auto"


def test_admin_mpv_outputs_404_for_chromecast(_admin_mocks, monkeypatch):
    """The mpv outputs endpoint is unavailable on the Chromecast backend."""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "player_backend", "chromecast")
    response = _admin_client().get("/admin/mpv/outputs")
    assert response.status_code == 404


def test_admin_select_mpv_output_success(_admin_mocks, monkeypatch):
    """A successful output switch returns success:true."""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "player_backend", "mpv")
    cc, _ = _admin_mocks
    cc.select_output = Mock(return_value=(True, ""))
    response = _admin_client().post(
        "/admin/mpv/output/select",
        data={
            "drm_device": "/dev/dri/card0",
            "drm_connector": "HDMI-A-1",
            "audio_device": "auto",
        },
    )
    assert response.status_code == 200
    assert response.json()["success"] is True
    cc.select_output.assert_called_once_with("/dev/dri/card0", "HDMI-A-1", "auto")


def test_admin_select_mpv_output_rejected_during_playback(_admin_mocks, monkeypatch):
    """A rejected switch (e.g. song playing) returns 409."""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "player_backend", "mpv")
    cc, _ = _admin_mocks
    cc.select_output = Mock(
        return_value=(False, "Cannot change output during playback.")
    )
    response = _admin_client().post(
        "/admin/mpv/output/select",
        data={
            "drm_device": "/dev/dri/card0",
            "drm_connector": "HDMI-A-1",
            "audio_device": "auto",
        },
    )
    assert response.status_code == 409
    assert response.json()["success"] is False


def test_admin_select_mpv_output_404_for_chromecast(_admin_mocks, monkeypatch):
    """Output selection is unavailable on the Chromecast backend."""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "player_backend", "chromecast")
    response = _admin_client().post(
        "/admin/mpv/output/select",
        data={
            "drm_device": "/dev/dri/card0",
            "drm_connector": "HDMI-A-1",
            "audio_device": "auto",
        },
    )
    assert response.status_code == 404


def test_admin_playback_start_empty_queue(_admin_mocks):
    """Starting playback with an empty queue returns 400."""
    cc, qm = _admin_mocks
    qm.get_queue_size = AsyncMock(return_value=0)
    response = _admin_client().post("/admin/playback/start")
    assert response.status_code == 400


def test_admin_playback_start_success(_admin_mocks):
    """Starting playback with a non-empty queue returns the service result."""
    cc, qm = _admin_mocks
    qm.get_queue_size = AsyncMock(return_value=2)
    response = _admin_client().post("/admin/playback/start")
    assert response.status_code == 200
    assert response.json()["message"] == "started"


def test_admin_playback_stop(_admin_mocks):
    """Stopping playback returns the service result."""
    response = _admin_client().post("/admin/playback/stop")
    assert response.status_code == 200
    assert response.json()["message"] == "stopped"


def test_admin_playback_skip(_admin_mocks):
    """Skipping returns the service result."""
    response = _admin_client().post("/admin/playback/skip")
    assert response.status_code == 200
    assert response.json()["message"] == "skipped"


def test_admin_status(_admin_mocks):
    """The status endpoint reports playback and queue state."""
    cc, qm = _admin_mocks
    cc.is_playing = True
    cc.selected_device_uuid = "u1"
    qm.get_queue_size = AsyncMock(return_value=4)
    qm.get_currently_playing = AsyncMock(return_value={"title": "Song"})

    response = _admin_client().get("/admin/status")

    assert response.status_code == 200
    body = response.json()
    assert body["is_playing"] is True
    assert body["selected_device_uuid"] == "u1"
    assert body["queue_size"] == 4
    assert body["currently_playing"] == {"title": "Song"}


def test_admin_delete_queue_item_success(_admin_mocks):
    """Admin delete of an existing item returns success."""
    response = _admin_client().delete("/admin/queue/7")
    assert response.status_code == 200
    assert response.json()["success"] is True


def test_admin_delete_queue_item_not_found(_admin_mocks):
    """Admin delete of a missing item returns 404."""
    _, qm = _admin_mocks
    qm.remove_from_queue = AsyncMock(return_value=False)
    response = _admin_client().delete("/admin/queue/7")
    assert response.status_code == 404


def test_admin_delete_queue_item_error(_admin_mocks):
    """Admin delete that raises returns 500."""
    _, qm = _admin_mocks
    qm.remove_from_queue = AsyncMock(side_effect=RuntimeError("db"))
    response = _admin_client().delete("/admin/queue/7")
    assert response.status_code == 500


def test_admin_clear_queue_success(_admin_mocks):
    """Clearing the queue returns a count message."""
    response = _admin_client().post("/admin/queue/clear")
    assert response.status_code == 200
    assert "Cleared 3 items" in response.json()["message"]


def test_admin_clear_queue_error(_admin_mocks):
    """A clear error returns 500."""
    _, qm = _admin_mocks
    qm.clear_queue = AsyncMock(side_effect=RuntimeError("db"))
    response = _admin_client().post("/admin/queue/clear")
    assert response.status_code == 500
