"""
Zendesk API client - fetch ticket info and conversation history.

Auth: uses email/token authentication (API token).
Docs: https://developer.zendesk.com/api-reference/ticketing/tickets/ticket_comments
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx

from app.config import get_settings

logger = logging.getLogger("jarvis.zendesk")

ZENDESK_SUBDOMAIN = os.environ.get("ZENDESK_SUBDOMAIN", "nicebuildllc")
ZENDESK_EMAIL = os.environ.get("ZENDESK_EMAIL", "")
ZENDESK_API_TOKEN = os.environ.get("ZENDESK_API_TOKEN", "")


def _get_auth() -> Optional[tuple]:
    """Return (email/token, api_token) for Zendesk basic auth."""
    if not ZENDESK_EMAIL or not ZENDESK_API_TOKEN:
        return None
    return (f"{ZENDESK_EMAIL}/token", ZENDESK_API_TOKEN)


def extract_ticket_id(text: str) -> Optional[str]:
    """Extract ticket ID from URL or number."""
    if not text:
        return None
    m = re.search(r"tickets/(\d+)", text)
    if m:
        return m.group(1)
    m = re.search(r"#?(\d{4,})", text)
    if m:
        return m.group(1)
    return None


async def fetch_ticket(ticket_id: str) -> Dict[str, Any]:
    """Fetch ticket details from Zendesk."""
    auth = _get_auth()
    if not auth:
        raise RuntimeError("Zendesk API credentials not configured (ZENDESK_EMAIL + ZENDESK_API_TOKEN)")

    url = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2/tickets/{ticket_id}.json"
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.get(url, auth=auth)
        resp.raise_for_status()
        return resp.json().get("ticket", {})


async def fetch_ticket_comments(ticket_id: str, max_comments: int = 50) -> List[Dict[str, Any]]:
    """
    Fetch ticket comments (conversation history) from Zendesk.
    Returns the most recent `max_comments` comments.
    """
    auth = _get_auth()
    if not auth:
        raise RuntimeError("Zendesk API credentials not configured (ZENDESK_EMAIL + ZENDESK_API_TOKEN)")

    url = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2/tickets/{ticket_id}/comments.json?sort_order=desc&per_page={max_comments}"
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.get(url, auth=auth)
        resp.raise_for_status()
        data = resp.json()

    comments = data.get("comments", [])
    # Reverse to chronological order (oldest first)
    comments.reverse()

    result = []
    for c in comments:
        body = c.get("body", "").strip()
        if not body:
            continue
        result.append({
            "id": c.get("id"),
            "author_id": c.get("author_id"),
            "public": c.get("public", True),
            "body": body[:2000],  # cap individual comment length
            "created_at": c.get("created_at", ""),
        })

    return result[:max_comments]


async def fetch_ticket_with_comments(ticket_id: str, max_comments: int = 50) -> Dict[str, Any]:
    """Fetch ticket + comments in one call."""
    ticket = await fetch_ticket(ticket_id)
    comments = await fetch_ticket_comments(ticket_id, max_comments)

    return {
        "ticket_id": ticket_id,
        "subject": ticket.get("subject", ""),
        "description": ticket.get("description", "")[:1000],
        "status": ticket.get("status", ""),
        "priority": ticket.get("priority", ""),
        "tags": ticket.get("tags", []),
        "comments": comments,
        "comment_count": len(comments),
    }
