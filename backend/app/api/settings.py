"""
API routes for system settings / agent configuration.
"""

from __future__ import annotations

import json
import logging
from typing import List

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import get_settings
from app.db import database as db
from app.models.schemas import AgentConfigUpdate

logger = logging.getLogger("jarvis.api.settings")
router = APIRouter()

# Default fixed members for escalation groups
DEFAULT_ESCALATION_MEMBERS = [
    "sanato.zhang@plaud.ai",
    "leon@plaud.ai",
    "yang@plaud.ai",
    "will.wu@plaud.ai",
    "david.liu@plaud.ai",
    "lucy.ding@plaud.ai",
]


@router.get("/agent")
async def get_agent_config():
    """Get current agent configuration."""
    settings = get_settings()
    ag = settings.agent

    providers = {}
    for name, p in ag.providers.items():
        providers[name] = {
            "enabled": p.enabled,
            "model": p.model,
            "timeout": p.timeout,
            "max_turns": ag.max_turns,
            "allowed_tools": p.allowed_tools,
        }

    return {
        "default": ag.default,
        "timeout": ag.timeout,
        "max_turns": ag.max_turns,
        "providers": providers,
        "routing": ag.routing,
    }


@router.put("/agent")
async def update_agent_config(req: AgentConfigUpdate):
    """Update agent configuration (runtime only, not persisted to yaml)."""
    settings = get_settings()

    if req.default_agent is not None:
        settings.agent.default = req.default_agent
    if req.timeout is not None:
        settings.agent.timeout = req.timeout
    if req.max_turns is not None:
        settings.agent.max_turns = req.max_turns
    if req.routing is not None:
        settings.agent.routing.update(req.routing)

    return {"status": "updated", "agent": settings.agent.default}


@router.get("/concurrency")
async def get_concurrency_config():
    """Get concurrency configuration."""
    settings = get_settings()
    return {
        "max_workers": settings.concurrency.max_workers,
        "max_agent_sessions": settings.concurrency.max_agent_sessions,
        "max_downloads": settings.concurrency.max_downloads,
        "task_timeout": settings.concurrency.task_timeout,
    }


# ---------------------------------------------------------------------------
# Escalation fixed members
# ---------------------------------------------------------------------------

ESCALATION_MEMBERS_KEY = "escalation_fixed_members"


@router.get("/escalation-members")
async def get_escalation_members():
    """Get fixed members that are always added to escalation groups."""
    raw = await db.get_oncall_config(ESCALATION_MEMBERS_KEY, "")
    if raw:
        members = json.loads(raw)
    else:
        members = DEFAULT_ESCALATION_MEMBERS
    return {"members": members}


class EscalationMembersUpdate(BaseModel):
    members: List[str]


@router.put("/escalation-members")
async def update_escalation_members(req: EscalationMembersUpdate):
    """Update the fixed members list for escalation groups."""
    cleaned = [e.strip() for e in req.members if e.strip()]
    await db.set_oncall_config(ESCALATION_MEMBERS_KEY, json.dumps(cleaned, ensure_ascii=False))
    return {"status": "updated", "members": cleaned}
