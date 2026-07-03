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


@router.get("/config")
async def auth_config():
    """Public, no-auth endpoint so the frontend can decide whether to gate on
    Feishu login *before* knowing if the visitor has ever authenticated —
    `/me` returns 401 both when SSO is on but the visitor is new, and when SSO
    is off entirely, so it can't be used to distinguish the two.
    """
    settings = get_settings().sso
    return {"sso_enabled": settings.enabled}


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


async def _exchange_code_for_user_info(code: str, redirect_uri: str = "") -> dict:
    """Exchange auth code → user_info dict.

    `redirect_uri` MUST match the one used in the authorize step (OAuth spec).
    Defaults to settings.feishu_redirect_uri for the regular login flow; bind
    flow passes the bind-callback URL to satisfy Feishu's strict equality check.
    """
    settings = get_settings().sso
    effective_redirect = redirect_uri or settings.feishu_redirect_uri
    async with httpx.AsyncClient(timeout=10) as ac:
        token_resp = await ac.post(
            FEISHU_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": settings.feishu_app_id,
                "client_secret": settings.feishu_app_secret,
                "redirect_uri": effective_redirect,
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
        state_payload = verify_state(secret=settings.jwt_secret, state=state)
        next_url = state_payload["next"]
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

    # Look up by email first so a Feishu login lands on whatever account
    # already owns this email — whether that account was created via legacy
    # local registration or a prior Feishu login — instead of creating a
    # duplicate row keyed by a freshly-derived username.
    existing = await db.get_user_by_email(email)
    if not existing:
        # Legacy/ops-provisioned accounts (e.g. admins created by username
        # only, before feishu_email was ever populated) won't match on
        # email — fall back to the username Feishu login has always
        # derived, so we don't spin up a duplicate row for an account that
        # already exists under that name.
        existing = await db.get_user(derive_username_from_email(email))
    username = existing["username"] if existing else derive_username_from_email(email)
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


@router.get("/feishu/bind-login")
async def feishu_bind_login(request: Request, username: str, next: str = "/"):
    """Start Feishu OAuth for the purpose of binding an email to an existing username.

    Used in compat mode (ENABLE_SSO=false): user already has a localStorage
    username; this endpoint signs a state carrying that username, then sends
    them through Feishu OAuth. The bind-callback writes the returned email
    onto the existing user row instead of creating a new one.
    """
    settings = get_settings().sso
    if not username:
        return RedirectResponse("/?feishu_bind=error&reason=missing_username", status_code=302)
    state = sign_state(
        secret=settings.jwt_secret,
        next_url=next,
        bind_username=username,
    )
    params = {
        "app_id": settings.feishu_app_id,
        "redirect_uri": settings.feishu_redirect_uri.replace(
            "/api/auth/feishu/callback", "/api/auth/feishu/bind-callback"
        ),
        "state": state,
    }
    return RedirectResponse(
        f"{FEISHU_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}",
        status_code=302,
    )


@router.get("/feishu/bind-callback")
async def feishu_bind_callback(request: Request, code: Optional[str] = None,
                                state: Optional[str] = None):
    """Receive Feishu OAuth callback for bind flow; write email to existing user.

    Differs from /feishu/callback in:
      - Does NOT create or upsert by derived username.
      - Updates feishu_email on the user named in `state.bind_username`.
      - Does NOT set the auth cookie (legacy login still uses localStorage).
      - Returns 302 to next_url with success/error flag in query.
    """
    settings = get_settings().sso

    if not code or not state:
        return RedirectResponse("/?feishu_bind=error&reason=invalid_state", status_code=302)

    try:
        payload = verify_state(secret=settings.jwt_secret, state=state)
    except StateError:
        return RedirectResponse("/?feishu_bind=error&reason=invalid_state", status_code=302)

    target_username = payload.get("bind_username", "")
    next_url = payload.get("next", "/")
    if not target_username:
        return RedirectResponse(f"{next_url}?feishu_bind=error&reason=missing_username", status_code=302)

    bind_redirect_uri = settings.feishu_redirect_uri.replace(
        "/api/auth/feishu/callback", "/api/auth/feishu/bind-callback"
    )
    try:
        user_info = await _exchange_code_for_user_info(code, redirect_uri=bind_redirect_uri)
    except Exception as e:
        logger.error("bind_oauth_network_error err=%s", e)
        return RedirectResponse(f"{next_url}?feishu_bind=error&reason=oauth_failed", status_code=302)

    email = (user_info.get("enterprise_email") or user_info.get("email") or "").lower().strip()
    if not email:
        return RedirectResponse(f"{next_url}?feishu_bind=error&reason=no_email", status_code=302)

    domain = email.rsplit("@", 1)[-1]
    if domain not in settings.allowed_domains:
        logger.warning("bind_login_rejected_domain email=%s", email)
        return RedirectResponse(f"{next_url}?feishu_bind=error&reason=domain_not_allowed", status_code=302)

    result = await db.update_user_feishu_email(target_username, email)
    if not result:
        logger.warning("bind_target_user_not_found username=%s", target_username)
        return RedirectResponse(f"{next_url}?feishu_bind=error&reason=user_not_found", status_code=302)

    logger.info("bind_success username=%s email=%s", target_username, email)
    return RedirectResponse(f"{next_url}?feishu_bind=ok&email={urllib.parse.quote(email)}", status_code=302)


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
