"""Unit tests for settings validation (placeholder and emptiness checks)."""

import pytest
from pydantic import ValidationError

from app.config import Settings

VALID_SECRET = "x" * 40


def _make(**overrides):
    """Build Settings with valid defaults, applying overrides.

    Passes _env_file=None so the test does not read the developer's real .env
    (which may override defaults like MAX_QUEUE_SIZE).
    """
    kwargs = {
        "admin_password": "a-real-password",
        "youtube_api_key": "a-real-key",
        "secret_key": VALID_SECRET,
    }
    kwargs.update(overrides)
    return Settings(_env_file=None, **kwargs)


def test_valid_settings_load():
    """A fully valid configuration constructs successfully."""
    settings = _make()
    assert settings.admin_password == "a-real-password"
    assert settings.secret_key == VALID_SECRET


def test_placeholder_admin_password_rejected():
    """The example docker-compose admin password is refused."""
    with pytest.raises(ValidationError):
        _make(admin_password="your_secure_password_here")


def test_placeholder_secret_key_rejected():
    """The example docker-compose secret key is refused."""
    with pytest.raises(ValidationError):
        _make(secret_key="your_secret_key_here")


def test_short_secret_key_rejected():
    """A secret key under 32 chars is refused."""
    with pytest.raises(ValidationError):
        _make(secret_key="too-short")


def test_empty_admin_password_rejected():
    """An empty admin password is refused."""
    with pytest.raises(ValidationError):
        _make(admin_password="   ")
