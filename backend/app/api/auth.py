"""/api/auth/* — Feishu SSO login flow + session inspection.

Endpoints:
    GET  /feishu/login     302 → Feishu authorize URL
    GET  /feishu/callback  302 → frontend (with Set-Cookie)
    GET  /me               200 → current user JSON
    POST /logout           204 → clear cookie

Feishu OAuth V2 (form-encoded, OAuth-standard):
    Authorize: https://accounts.feishu.cn/open-apis/authen/v1/authorize
    Token:     https://open.feishu.cn/open-apis/authen/v2/oauth/token
    UserInfo:  https://open.feishu.cn/open-apis/authen/v1/user_info
"""

from __future__ import annotations

import logging
import urllib.parse
from typing import Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.config import get_settings
from app.db import database as db
from app.services.auth_feishu import (
    StateError,
    derive_username_from_email,
    sign_state,
    verify_state,
)
from app.services.auth_jwt import sign_token, verify_token, JWTError


logger = logging.getLogger("jarvis.api.auth")
router = APIRouter()


FEISHU_AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
FEISHU_USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"


def _login_error_redirect(error_code: str) -> RedirectResponse:
    return RedirectResponse(f"/login?error={error_code}", status_code=302)


@router.get("/feishu/login")
async def feishu_login(request: Request, next: str = "/"):
    settings = get_settings().sso
    state = sign_state(secret=settings.jwt_secret, next_url=next)
    params = {
        "app_id": settings.feishu_app_id,
        "redirect_uri": settings.feishu_redirect_uri,
        "state": state,
    }
    return RedirectResponse(
        f"{FEISHU_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}",
        status_code=302,
    )


async def _exchange_code_for_user_info(code: str) -> dict:
    """Exchange auth code → user_info dict.

    Pulled out for test mocking. Two-step:
      1. POST token endpoint with code → access_token
      2. GET user_info with Bearer access_token → {email, enterprise_email, name, ...}
    """
    settings = get_settings().sso
    async with httpx.AsyncClient(timeout=10) as ac:
        token_resp = await ac.post(
            FEISHU_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": settings.feishu_app_id,
                "client_secret": settings.feishu_app_secret,
                "redirect_uri": settings.feishu_redirect_uri,
            },
        )
        token_resp.raise_for_status()
        token_body = token_resp.json()
        # Feishu returns either OAuth-standard {access_token, ...} or {code, data: {...}}
        if "access_token" in token_body:
            access_token = token_body["access_token"]
        elif token_body.get("code") == 0 and isinstance(token_body.get("data"), dict):
            access_token = token_body["data"]["access_token"]
        else:
            raise RuntimeError(f"feishu token error: {token_body}")

        info_resp = await ac.get(
            FEISHU_USER_INFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        info_resp.raise_for_status()
        info_body = info_resp.json()
        # Feishu wraps: {code: 0, msg: "ok", data: {email, enterprise_email, name, open_id, ...}}
        if info_body.get("code") != 0 or not isinstance(info_body.get("data"), dict):
            raise RuntimeError(f"feishu user_info error: {info_body}")
        return info_body["data"]


@router.get("/feishu/callback")
async def feishu_callback(request: Request, code: Optional[str] = None,
                          state: Optional[str] = None):
    settings = get_settings().sso

    if not code or not state:
        return _login_error_redirect("invalid_state")

    try:
        next_url = verify_state(secret=settings.jwt_secret, state=state)
    except StateError:
        return _login_error_redirect("invalid_state")

    try:
        user_info = await _exchange_code_for_user_info(code)
    except Exception as e:
        logger.error("sso_oauth_network_error err=%s", e)
        return _login_error_redirect("oauth_failed")

    # Prefer enterprise_email (work email), fallback to email.
    email = (user_info.get("enterprise_email") or user_info.get("email") or "").lower().strip()
    if not email:
        logger.warning("sso_no_email user_info_keys=%s", list(user_info.keys()))
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
    settings = get_settings().sso
    user_state = getattr(request.state, "user", None)
    if not user_state:
        token = request.cookies.get(settings.cookie_name)
        if token:
            try:
                payload = verify_token(token, secret=settings.jwt_secret)
                user_state = {
                    "username": payload["username"],
                    "email": payload["email"],
                    "role": payload["role"],
                }
            except JWTError:
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
