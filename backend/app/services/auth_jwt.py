"""JWT sign/verify helpers for SSO session cookies.

Centralizes algorithm, claim shape, and error normalization. Callers should
import only `sign_token`, `verify_token`, `JWTError` from this module.
"""

from __future__ import annotations

import time

import jwt as _pyjwt
from jwt.exceptions import PyJWTError


ALGORITHM = "HS256"


class JWTError(Exception):
    """Raised on any JWT failure (bad signature, expired, malformed)."""


def sign_token(
    *,
    secret: str,
    username: str,
    email: str,
    role: str,
    ttl_seconds: int,
) -> str:
    now = int(time.time())
    payload = {
        "username": username,
        "email": email,
        "role": role,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return _pyjwt.encode(payload, secret, algorithm=ALGORITHM)


def verify_token(token: str, *, secret: str) -> dict:
    try:
        return _pyjwt.decode(token, secret, algorithms=[ALGORITHM])
    except PyJWTError as e:
        raise JWTError(str(e)) from e
