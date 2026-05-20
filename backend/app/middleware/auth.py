"""Auth middleware.

Decides per-request whether to pass through, enforce JWT, or 401.
Does NOT couple to FastAPI settings — caller passes all config in __init__.
"""

from __future__ import annotations

import logging
from typing import List

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.services.auth_jwt import JWTError, verify_token


logger = logging.getLogger("jarvis.auth")


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        enabled: bool,
        cookie_name: str,
        jwt_secret: str,
        exempt_paths: List[str],
    ):
        super().__init__(app)
        self.enabled = enabled
        self.cookie_name = cookie_name
        self.jwt_secret = jwt_secret
        self.exempt_paths = tuple(exempt_paths)

    async def dispatch(self, request: Request, call_next) -> Response:
        if not self.enabled:
            return await call_next(request)

        path = request.url.path
        if self._is_exempt(path):
            return await call_next(request)

        token = request.cookies.get(self.cookie_name)
        if not token:
            logger.debug("auth_rejected path=%s reason=no_cookie", path)
            return JSONResponse({"detail": "unauthenticated"}, status_code=401)

        try:
            payload = verify_token(token, secret=self.jwt_secret)
        except JWTError as e:
            logger.debug("auth_rejected path=%s reason=%s", path, e)
            return JSONResponse({"detail": "invalid_or_expired_token"}, status_code=401)

        request.state.user = {
            "username": payload["username"],
            "email": payload["email"],
            "role": payload["role"],
        }
        return await call_next(request)

    def _is_exempt(self, path: str) -> bool:
        return any(path.startswith(p) for p in self.exempt_paths)
