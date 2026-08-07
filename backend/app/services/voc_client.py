"""
VOC Portal API client — https://voc-portal-apse1.nicebuild.click

Auth: Keycloak (sso.nicebuild.click/realms/plaud) client_credentials grant against
VOC's own token endpoint (/oauth/token). Token is cached in-process and refreshed
shortly before expiry.

Only reads taxonomy tags (GET /api/taxonomy/tags) — this integration doesn't write
back to VOC (tag editing stays in the VOC portal UI, owned by the VoC team).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from app.config import get_settings

logger = logging.getLogger("jarvis.voc_client")

# Refresh this many seconds before actual expiry, to avoid racing a token that
# expires mid-request.
_TOKEN_REFRESH_MARGIN_SECONDS = 30


class VocCredentialsMissing(RuntimeError):
    """Raised when client_id/client_secret aren't configured. Fail loud, not silent."""


class VocAuthError(RuntimeError):
    """Raised when the token endpoint rejects our credentials."""


class VocApiError(RuntimeError):
    """Raised when a VOC API call fails (non-2xx, unexpected shape)."""


# Module-level cache — one token shared across all callers in this process.
# Not thread-safe across multiple worker processes, but this backend runs
# single-process (see backend/CLAUDE.md); good enough for a low-frequency
# (daily) sync job.
_cached_token: Optional[str] = None
_cached_token_expires_at: float = 0.0


async def _fetch_token() -> str:
    global _cached_token, _cached_token_expires_at

    now = time.time()
    if _cached_token and now < _cached_token_expires_at - _TOKEN_REFRESH_MARGIN_SECONDS:
        return _cached_token

    settings = get_settings().voc
    if not settings.client_id or not settings.client_secret:
        raise VocCredentialsMissing(
            "VOC_CLIENT_ID / VOC_CLIENT_SECRET not set — cannot authenticate to VOC Portal. "
            "Set them in .env once a service account is provisioned."
        )

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            settings.token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": settings.client_id,
                "client_secret": settings.client_secret,
            },
        )
    if resp.status_code != 200:
        raise VocAuthError(
            f"VOC token endpoint returned {resp.status_code}: {resp.text[:500]}"
        )

    payload = resp.json()
    token = payload.get("access_token")
    if not token:
        raise VocAuthError(f"VOC token response missing access_token: {payload}")

    expires_in = payload.get("expires_in", 300)
    _cached_token = token
    _cached_token_expires_at = now + float(expires_in)
    return token


async def fetch_taxonomy_tags() -> List[Dict[str, Any]]:
    """GET /api/taxonomy/tags — the full list of *active* tags.

    VOC's own UI (`n active tags` label) confirms this endpoint returns only
    non-retired tags; retirement is inferred locally by absence (see
    app.services.voc_taxonomy.sync_from_voc).
    """
    settings = get_settings().voc
    token = await _fetch_token()

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{settings.base_url.rstrip('/')}/api/taxonomy/tags",
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code == 401:
        raise VocAuthError("VOC rejected the access token (401) fetching /api/taxonomy/tags")
    if resp.status_code != 200:
        raise VocApiError(
            f"GET /api/taxonomy/tags returned {resp.status_code}: {resp.text[:500]}"
        )

    data = resp.json()
    if not isinstance(data, list):
        raise VocApiError(f"Expected a JSON array from /api/taxonomy/tags, got {type(data)}")
    return data
