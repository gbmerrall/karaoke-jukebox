"""Unit tests for the remaining app.config branches.

Exercises the path helpers, environment detection, host/IP resolution, video
URL generation, and the field validators that are not covered by
test_config_validation.py.
"""

import socket

import pytest
from pydantic import ValidationError

from app.config import Settings

VALID_SECRET = "x" * 40


def _make(**overrides) -> Settings:
    """Build a Settings instance with valid defaults, applying overrides.

    Passes _env_file=None so the developer's real .env is never read.

    Args:
        **overrides: Field values to override on the constructed Settings.

    Returns:
        A constructed Settings instance.
    """
    kwargs = {
        "admin_password": "a-real-password",
        "youtube_api_key": "a-real-key",
        "secret_key": VALID_SECRET,
    }
    kwargs.update(overrides)
    return Settings(_env_file=None, **kwargs)


# Path helpers


def test_get_db_path():
    """The DB path sits under the configured data dir."""
    settings = _make(data_dir="/tmp/karaoke-data")
    assert str(settings.get_db_path()).endswith("/tmp/karaoke-data/karaoke.db")


def test_get_videos_dir():
    """The videos dir sits under the configured data dir."""
    settings = _make(data_dir="/tmp/karaoke-data")
    assert str(settings.get_videos_dir()).endswith("/tmp/karaoke-data/videos")


def test_get_video_path():
    """A video path joins the videos dir with the id and .mp4 extension."""
    settings = _make(data_dir="/tmp/karaoke-data")
    assert str(settings.get_video_path("abc12345678")).endswith(
        "/tmp/karaoke-data/videos/abc12345678.mp4"
    )


# Docker detection


def test_is_docker_true(monkeypatch):
    """is_docker returns True when /.dockerenv exists."""
    import app.config as config_module

    class _FakePath:
        def __init__(self, _path):
            pass

        def exists(self):
            return True

    monkeypatch.setattr(config_module, "Path", _FakePath)
    assert _make().is_docker() is True


def test_is_docker_false(monkeypatch):
    """is_docker returns False when /.dockerenv is absent."""
    import app.config as config_module

    class _FakePath:
        def __init__(self, _path):
            pass

        def exists(self):
            return False

    monkeypatch.setattr(config_module, "Path", _FakePath)
    assert _make().is_docker() is False


# Local IP detection


def test_get_local_ip_success(monkeypatch):
    """A working socket yields the address from getsockname."""

    class _FakeSocket:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self, _addr):
            return None

        def getsockname(self):
            return ("192.168.1.42", 12345)

        def close(self):
            return None

    monkeypatch.setattr(socket, "socket", _FakeSocket)
    assert _make().get_local_ip() == "192.168.1.42"


def test_get_local_ip_failure(monkeypatch):
    """A socket error falls back to 'localhost'."""

    def _boom(*args, **kwargs):
        raise OSError("no network")

    monkeypatch.setattr(socket, "socket", _boom)
    assert _make().get_local_ip() == "localhost"


# Server host resolution


def test_get_server_host_explicit():
    """An explicit server_host is returned verbatim."""
    settings = _make(server_host="10.0.0.5")
    assert settings.get_server_host() == "10.0.0.5"


def test_get_server_host_autodetect(monkeypatch):
    """No server_host and not Docker -> auto-detected local IP.

    Settings is a pydantic model that forbids setting non-field attributes on an
    instance, so the helper methods are patched at the class level.
    """
    monkeypatch.setattr(Settings, "is_docker", lambda self: False)
    monkeypatch.setattr(Settings, "get_local_ip", lambda self: "172.16.0.9")
    settings = _make(server_host="")
    assert settings.get_server_host() == "172.16.0.9"


def test_get_server_host_docker_unset(monkeypatch):
    """No server_host but Docker -> empty string (requires explicit config)."""
    monkeypatch.setattr(Settings, "is_docker", lambda self: True)
    settings = _make(server_host="")
    assert settings.get_server_host() == ""


# Video URL generation


def test_get_video_url_with_host(monkeypatch):
    """A resolvable host produces a well-formed URL."""
    monkeypatch.setattr(Settings, "get_server_host", lambda self: "192.168.0.10")
    settings = _make(server_host="", server_port=9000)
    url = settings.get_video_url("abcdefghijk")
    assert url == "http://192.168.0.10:9000/data/videos/abcdefghijk.mp4"


def test_get_video_url_localhost_warns(monkeypatch):
    """An empty host falls back to localhost and still returns a URL."""
    monkeypatch.setattr(Settings, "get_server_host", lambda self: "")
    settings = _make(server_host="", server_port=8000)
    url = settings.get_video_url("abcdefghijk")
    assert url == "http://localhost:8000/data/videos/abcdefghijk.mp4"


def test_get_video_url_request_host_fallback(monkeypatch):
    """When server host is empty, the request host is used."""
    monkeypatch.setattr(Settings, "get_server_host", lambda self: "")
    settings = _make(server_host="", server_port=8000)
    url = settings.get_video_url("abcdefghijk", request_host="myhost.local")
    assert url == "http://myhost.local:8000/data/videos/abcdefghijk.mp4"


# Validators


def test_admin_password_placeholder_changeme_rejected():
    """The 'changeme' placeholder admin password is refused."""
    with pytest.raises(ValidationError):
        _make(admin_password="changeme")


def test_admin_password_too_short_rejected():
    """An admin password under 4 chars is refused."""
    with pytest.raises(ValidationError):
        _make(admin_password="abc")


def test_admin_password_valid_short_warning_path():
    """An 8-char password is valid (exercises the load-time warning floor)."""
    settings = _make(admin_password="eightchr")
    assert settings.admin_password == "eightchr"
    assert len(settings.admin_password) < 12


def test_youtube_api_key_empty_rejected():
    """An empty YouTube API key is refused."""
    with pytest.raises(ValidationError):
        _make(youtube_api_key="   ")


def test_secret_key_empty_rejected():
    """An empty secret key is refused."""
    with pytest.raises(ValidationError):
        _make(secret_key="   ")


def test_secret_key_placeholder_rejected():
    """A placeholder secret key is refused."""
    with pytest.raises(ValidationError):
        _make(secret_key="changeme")


def test_secret_key_too_short_rejected():
    """A secret key under 32 chars is refused."""
    with pytest.raises(ValidationError):
        _make(secret_key="x" * 31)


def test_invalid_log_level_rejected():
    """An unknown log level is refused."""
    with pytest.raises(ValidationError):
        _make(log_level="VERBOSE")


def test_log_level_normalised_to_upper():
    """A lowercase log level is accepted and upper-cased."""
    settings = _make(log_level="debug")
    assert settings.log_level == "DEBUG"


def test_empty_admin_password_rejected():
    """A whitespace-only admin password is refused."""
    with pytest.raises(ValidationError):
        _make(admin_password="   ")


# load_settings error handling


def test_load_settings_exits_on_validation_error(monkeypatch):
    """load_settings logs the errors and exits(1) on invalid configuration."""
    import app.config as config_module

    class _Boom:
        def __init__(self, *args, **kwargs):
            raise ValidationError.from_exception_data(
                "Settings",
                [{"type": "missing", "loc": ("admin_password",), "input": {}}],
            )

    monkeypatch.setattr(config_module, "Settings", _Boom)

    with pytest.raises(SystemExit) as exc:
        config_module.load_settings()
    assert exc.value.code == 1
