"""
API routes for user management and roles.
"""

from __future__ import annotations

import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import database as db

logger = logging.getLogger("jarvis.api.users")
router = APIRouter()


class UserLogin(BaseModel):
    username: str


@router.post("/login")
async def login_or_register(req: UserLogin):
    """Get or create a user. Returns user info with role."""
    if not req.username.strip():
        raise HTTPException(status_code=400, detail="Username is required")
    user = await db.get_or_create_user(req.username.strip())
    return user


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
