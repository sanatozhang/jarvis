"""/api/auth/* — Google SSO login flow + session inspection.

Endpoints:
    GET  /google/login     302 → Google authorize
    GET  /google/callback  302 → frontend (with Set-Cookie)
    GET  /me               200 → current user JSON
    POST /logout           204 → clear cookie
"""

from __future__ import annotations

import logging
import urllib.parse
from typing import Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests

from app.config import get_settings
from app.db import database as db
from app.services.auth_google import (
    StateError,
    derive_username_from_email,
    sign_state,
    verify_state,
)
from app.services.auth_jwt import sign_token


logger = logging.getLogger("jarvis.api.auth")
router = APIRouter()


GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def _login_error_redirect(error_code: str) -> RedirectResponse:
    return RedirectResponse(f"/login?error={error_code}", status_code=302)


@router.get("/google/login")
async def google_login(request: Request, next: str = "/"):
    settings = get_settings().sso
    state = sign_state(secret=settings.jwt_secret, next_url=next)
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
        "access_type": "online",
    }
    return RedirectResponse(
        f"{GOOGLE_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}",
        status_code=302,
    )


async def _exchange_code_for_id_token(code: str) -> dict:
    """Exchange auth code → verified ID token claims dict.

    Pulled out for test mocking. Returns a dict like {email, email_verified, sub, ...}.
    """
    settings = get_settings().sso
    async with httpx.AsyncClient(timeout=10) as ac:
        token_resp = await ac.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    token_resp.raise_for_status()
    id_token_str = token_resp.json()["id_token"]
    claims = google_id_token.verify_oauth2_token(
        id_token_str,
        google_requests.Request(),
        settings.google_client_id,
    )
    return claims


@router.get("/google/callback")
async def google_callback(request: Request, code: Optional[str] = None,
                          state: Optional[str] = None):
    settings = get_settings().sso

    if not code or not state:
        return _login_error_redirect("invalid_state")

    try:
        next_url = verify_state(secret=settings.jwt_secret, state=state)
    except StateError:
        return _login_error_redirect("invalid_state")

    try:
        claims = await _exchange_code_for_id_token(code)
    except Exception as e:
        logger.error("sso_oauth_network_error err=%s", e)
        return _login_error_redirect("oauth_failed")

    email = (claims.get("email") or "").lower()
    if not email or not claims.get("email_verified"):
        return _login_error_redirect("oauth_failed")

    domain = email.rsplit("@", 1)[-1]
    if domain not in settings.allowed_domains:
        logger.warning("sso_login_rejected_domain email=%s", email)
        return _login_error_redirect("domain_not_allowed")

    username = derive_username_from_email(email)
    existing = await db.get_user(username)
    is_env_admin = email in settings.admin_emails

    if existing:
        final_role = "admin" if (is_env_admin or existing["role"] == "admin") else existing["role"]
    else:
        final_role = "admin" if is_env_admin else "user"

    await db.upsert_user(username, feishu_email=email, role=final_role)

    if existing and existing.get("feishu_email") and existing["feishu_email"] != email:
        logger.info("feishu_email_changed username=%s old=%s new=%s",
                    username, existing["feishu_email"], email)
    if final_role == "admin" and (not existing or existing["role"] != "admin"):
        logger.warning("promote_to_admin username=%s via=env_whitelist", username)

    logger.info("sso_login_success username=%s email=%s role=%s",
                username, email, final_role)

    token = sign_token(
        secret=settings.jwt_secret,
        username=username,
        email=email,
        role=final_role,
        ttl_seconds=settings.cookie_days * 86400,
    )
    resp = RedirectResponse(next_url or "/", status_code=302)
    resp.set_cookie(
        key=settings.cookie_name,
        value=token,
        max_age=settings.cookie_days * 86400,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        domain=settings.cookie_domain or None,
        path="/",
    )
    return resp


@router.get("/me")
async def auth_me(request: Request):
    user_state = getattr(request.state, "user", None)
    if not user_state:
        # Fallback: parse cookie directly if middleware didn't (e.g. SSO disabled)
        settings = get_settings().sso
        token = request.cookies.get(settings.cookie_name)
        if token:
            try:
                from app.services.auth_jwt import verify_token
                payload = verify_token(token, secret=settings.jwt_secret)
                user_state = {
                    "username": payload["username"],
                    "email": payload["email"],
                    "role": payload["role"],
                }
            except Exception:
                pass
    if not user_state:
        return JSONResponse({"detail": "unauthenticated"}, status_code=401)
    record = await db.get_user(user_state["username"])
    return {
        "username": user_state["username"],
        "email": user_state["email"],
        "role": user_state["role"],
        "feishu_email": (record or {}).get("feishu_email", ""),
    }


@router.post("/logout")
async def logout(request: Request):
    settings = get_settings().sso
    resp = Response(status_code=204)
    resp.delete_cookie(
        key=settings.cookie_name,
        domain=settings.cookie_domain or None,
        path="/",
    )
    return resp
