"""Auth middleware.

Decides per-request whether to pass through, enforce JWT, or 401.

Two construction modes:

1. **Live mode** (production): pass ``settings_getter`` — a callable returning the
   current SSO settings on every dispatch. This allows tests / runtime to flip
   ``settings.sso.enabled`` and have the middleware honor the change without
   reconstruction.

2. **Static mode** (legacy / focused tests): pass ``enabled`` / ``cookie_name``
   / ``jwt_secret`` / ``exempt_paths`` directly. Config is frozen at construction
   time. Used by ``tests/test_auth_middleware.py`` to isolate middleware logic
   from the global settings singleton.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, List, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.services.auth_jwt import JWTError, verify_token


logger = logging.getLogger("jarvis.auth")


class _StaticConfig:
    """Frozen config snapshot for static-mode construction."""

    __slots__ = ("enabled", "cookie_name", "jwt_secret", "exempt_paths")

    def __init__(
        self,
        *,
        enabled: bool,
        cookie_name: str,
        jwt_secret: str,
        exempt_paths: tuple,
    ):
        self.enabled = enabled
        self.cookie_name = cookie_name
        self.jwt_secret = jwt_secret
        self.exempt_paths = exempt_paths


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        settings_getter: Optional[Callable[[], Any]] = None,
        # Legacy / static-mode args (used by test_auth_middleware.py)
        enabled: Optional[bool] = None,
        cookie_name: Optional[str] = None,
        jwt_secret: Optional[str] = None,
        exempt_paths: Optional[List[str]] = None,
    ):
        super().__init__(app)
        self._getter: Optional[Callable[[], Any]] = settings_getter
        self._static: Optional[_StaticConfig] = None
        if settings_getter is None:
            # Static mode — freeze provided values
            self._static = _StaticConfig(
                enabled=bool(enabled),
                cookie_name=cookie_name or "jarvis_session",
                jwt_secret=jwt_secret or "",
                exempt_paths=tuple(exempt_paths or []),
            )

    def _config(self) -> Any:
        return self._getter() if self._getter is not None else self._static

    async def dispatch(self, request: Request, call_next) -> Response:
        cfg = self._config()
        if not cfg.enabled:
            return await call_next(request)

        path = request.url.path
        if any(path.startswith(p) for p in cfg.exempt_paths):
            return await call_next(request)

        token = request.cookies.get(cfg.cookie_name)
        if not token:
            logger.debug("auth_rejected path=%s reason=no_cookie", path)
            return JSONResponse({"detail": "unauthenticated"}, status_code=401)

        try:
            payload = verify_token(token, secret=cfg.jwt_secret)
        except JWTError as e:
            logger.debug("auth_rejected path=%s reason=%s", path, e)
            return JSONResponse({"detail": "invalid_or_expired_token"}, status_code=401)

        request.state.user = {
            "username": payload["username"],
            "email": payload["email"],
            "role": payload["role"],
        }
        return await call_next(request)
