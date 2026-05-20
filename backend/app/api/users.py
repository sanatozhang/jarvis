"""
API routes for user management and roles.
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import database as db

logger = logging.getLogger("jarvis.api.users")
router = APIRouter()

ALLOWED_EMAIL_DOMAIN = "@plaud.ai"
EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@plaud\.ai$")


class UserLogin(BaseModel):
    username: str
    email: Optional[str] = None


@router.post("/login")
async def login_or_register(req: UserLogin):
    """Login or register. New users MUST provide a @plaud.ai email."""
    username = req.username.strip().lower()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")

    existing = await db.get_user(username)

    email_input = (req.email or "").strip().lower()
    if email_input and not EMAIL_RE.match(email_input):
        raise HTTPException(
            status_code=400,
            detail=f"Email must end with {ALLOWED_EMAIL_DOMAIN}",
        )

    if existing:
        if email_input and email_input != (existing.get("feishu_email") or ""):
            await db.update_user_feishu_email(username, email_input)
            existing["feishu_email"] = email_input
        return existing

    if not email_input:
        raise HTTPException(
            status_code=400,
            detail=f"New users must register with a {ALLOWED_EMAIL_DOMAIN} email",
        )
    return await db.upsert_user(username, feishu_email=email_input)


@router.get("/{username}")
async def get_user(username: str):
    """Get user info."""
    user = await db.get_user(username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.get("")
async def list_users():
    """List all users (admin only in practice, no enforcement yet)."""
    return await db.list_users()
