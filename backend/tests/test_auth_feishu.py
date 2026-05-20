"""Feishu OAuth helpers - state HMAC + username derivation."""
from __future__ import annotations

import pytest

from app.services.auth_feishu import (
    derive_username_from_email,
    sign_state,
    verify_state,
    StateError,
)


SECRET = "x" * 64


def test_derive_username_basic():
    assert derive_username_from_email("sanato.zhang@plaud.ai") == "sanato.zhang"


def test_derive_username_strips_alias():
    assert derive_username_from_email("foo+linear@plaud.ai") == "foo"


def test_derive_username_lowercases():
    assert derive_username_from_email("Sanato.Zhang@plaud.ai") == "sanato.zhang"


def test_derive_username_sanitizes_chars():
    assert derive_username_from_email("weird#name@plaud.ai") == "weird_name"


def test_state_signing_roundtrip():
    state = sign_state(secret=SECRET, next_url="/issues/123")
    payload = verify_state(secret=SECRET, state=state)
    assert payload["next"] == "/issues/123"


def test_state_rejects_tampered():
    state = sign_state(secret=SECRET, next_url="/")
    tampered = state[:-2] + "xx"
    with pytest.raises(StateError):
        verify_state(secret=SECRET, state=tampered)


def test_state_rejects_wrong_secret():
    state = sign_state(secret=SECRET, next_url="/")
    with pytest.raises(StateError):
        verify_state(secret="y" * 64, state=state)


def test_state_ignores_external_next_url():
    state = sign_state(secret=SECRET, next_url="https://evil.com/x")
    assert verify_state(secret=SECRET, state=state)["next"] == "/"


def test_state_carries_extra_payload():
    state = sign_state(secret=SECRET, next_url="/", target_username="sanato")
    payload = verify_state(secret=SECRET, state=state)
    assert payload["next"] == "/"
    assert payload["target_username"] == "sanato"
