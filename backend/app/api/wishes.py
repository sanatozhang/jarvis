"""
Wishes API — feature request / wish pool.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db.database import WishRecord, get_session

logger = logging.getLogger("jarvis.api.wishes")
router = APIRouter()


class WishCreate(BaseModel):
    title: str
    description: str = ""
    created_by: str = ""


class WishUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None


@router.get("")
async def list_wishes():
    """List all wishes ordered by votes then recency."""
    async with get_session() as session:
        from sqlalchemy import select
        stmt = select(WishRecord).order_by(
            WishRecord.votes.desc(), WishRecord.created_at.desc()
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [_to_dict(r) for r in rows]


@router.post("")
async def create_wish(req: WishCreate):
    """Create a new wish."""
    if not req.title.strip():
        raise HTTPException(400, "title is required")
    async with get_session() as session:
        record = WishRecord(
            title=req.title.strip(),
            description=req.description.strip(),
            created_by=req.created_by,
            created_at=datetime.utcnow(),
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return _to_dict(record)


@router.put("/{wish_id}")
async def update_wish(wish_id: int, req: WishUpdate):
    """Update a wish (title, description, or status)."""
    async with get_session() as session:
        record = await session.get(WishRecord, wish_id)
        if not record:
            raise HTTPException(404, "wish not found")
        if req.title is not None:
            record.title = req.title.strip()
        if req.description is not None:
            record.description = req.description.strip()
        if req.status is not None:
            record.status = req.status
        await session.commit()
        await session.refresh(record)
        return _to_dict(record)


@router.post("/{wish_id}/vote")
async def vote_wish(wish_id: int):
    """Increment vote count for a wish."""
    async with get_session() as session:
        record = await session.get(WishRecord, wish_id)
        if not record:
            raise HTTPException(404, "wish not found")
        record.votes = (record.votes or 0) + 1
        await session.commit()
        await session.refresh(record)
        return _to_dict(record)


@router.delete("/{wish_id}")
async def delete_wish(wish_id: int):
    """Delete a wish."""
    async with get_session() as session:
        record = await session.get(WishRecord, wish_id)
        if not record:
            raise HTTPException(404, "wish not found")
        await session.delete(record)
        await session.commit()
        return {"deleted": wish_id}


def _to_dict(r: WishRecord) -> dict:
    return {
        "id": r.id,
        "title": r.title,
        "description": r.description,
        "status": r.status or "pending",
        "votes": r.votes or 0,
        "created_by": r.created_by or "",
        "created_at": r.created_at.isoformat() if r.created_at else "",
    }
