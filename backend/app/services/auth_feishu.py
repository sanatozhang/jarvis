"""Feishu OAuth flow helpers.

Two responsibilities:
1. derive_username_from_email — normalize Feishu enterprise_email → username PK.
2. sign_state / verify_state — CSRF protection + next_url carrier.

The actual OAuth HTTP flow (authorize URL, token exchange, user_info fetch)
runs through httpx in the API route; this module stays pure-Python testable.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re


class StateError(Exception):
    """Raised when OAuth state is missing, tampered, or signed by wrong key."""


def derive_username_from_email(email: str) -> str:
    local = email.split("@", 1)[0].lower().strip()
    local = local.split("+", 1)[0]
    local = re.sub(r"[^a-z0-9._-]", "_", local)
    return local[:64]


def _sanitize_next(next_url: str) -> str:
    if not next_url or not next_url.startswith("/") or next_url.startswith("//"):
        return "/"
    return next_url


def sign_state(*, secret: str, next_url: str = "/", **extra: str) -> str:
    payload = {"next": _sanitize_next(next_url)}
    for k, v in extra.items():
        if v is not None:
            payload[k] = str(v)
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    sig = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_state(*, secret: str, state: str) -> dict:
    if not state or "." not in state:
        raise StateError("state missing or malformed")
    body, sig = state.rsplit(".", 1)
    expected = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise StateError("state signature mismatch")
    try:
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, json.JSONDecodeError) as e:
        raise StateError("state body undecodable") from e
    payload["next"] = _sanitize_next(payload.get("next", "/"))
    return payload
