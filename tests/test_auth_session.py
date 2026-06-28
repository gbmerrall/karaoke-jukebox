"""Unit tests for signed, time-limited session cookies."""

from app.routes import auth


def test_round_trip():
    """A freshly signed session decodes back to the same data."""
    token = auth.encode_session({"username": "alice", "is_admin": True})
    assert auth.decode_session(token) == {"username": "alice", "is_admin": True}


def test_tampered_token_rejected():
    """Flipping a character invalidates the signature."""
    token = auth.encode_session({"username": "alice", "is_admin": False})
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert auth.decode_session(tampered) is None


def test_garbage_token_rejected():
    """Non-token input is rejected without raising (BadData covers BadPayload)."""
    assert auth.decode_session("not-a-real-token") is None


def test_token_signed_with_other_key_rejected():
    """A token signed with a different secret does not verify."""
    from itsdangerous import URLSafeTimedSerializer

    other = URLSafeTimedSerializer("a-different-secret-key-000000000000")
    forged = other.dumps({"username": "mallory", "is_admin": True})
    assert auth.decode_session(forged) is None


def test_expired_token_rejected(monkeypatch):
    """A token older than SESSION_MAX_AGE is rejected.

    Forcing SESSION_MAX_AGE to -1 makes any non-negative token age count as
    expired, exercising the timed-serializer path deterministically.
    """
    token = auth.encode_session({"username": "alice", "is_admin": False})
    monkeypatch.setattr(auth, "SESSION_MAX_AGE", -1)
    assert auth.decode_session(token) is None
