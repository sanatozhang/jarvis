"""JWT sign/verify roundtrip."""
from __future__ import annotations

import time
import pytest

from app.services.auth_jwt import sign_token, verify_token, JWTError


SECRET = "0123456789abcdef" * 4  # 64 chars


def test_sign_and_verify_roundtrip():
    token = sign_token(
        secret=SECRET,
        username="sanato.zhang",
        email="sanato.zhang@plaud.ai",
        role="admin",
        ttl_seconds=3600,
    )
    payload = verify_token(token, secret=SECRET)
    assert payload["username"] == "sanato.zhang"
    assert payload["email"] == "sanato.zhang@plaud.ai"
    assert payload["role"] == "admin"
    assert payload["exp"] > payload["iat"]


def test_verify_rejects_wrong_secret():
    token = sign_token(secret=SECRET, username="u", email="u@plaud.ai",
                        role="user", ttl_seconds=60)
    with pytest.raises(JWTError):
        verify_token(token, secret="different-secret-" * 4)


def test_verify_rejects_expired_token():
    token = sign_token(secret=SECRET, username="u", email="u@plaud.ai",
                        role="user", ttl_seconds=-10)
    with pytest.raises(JWTError):
        verify_token(token, secret=SECRET)


def test_verify_rejects_garbage():
    with pytest.raises(JWTError):
        verify_token("not.a.jwt", secret=SECRET)
