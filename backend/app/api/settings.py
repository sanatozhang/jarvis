"""
API routes for system settings / agent configuration.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from app.config import get_settings
from app.models.schemas import AgentConfigUpdate

logger = logging.getLogger("jarvis.api.settings")
router = APIRouter()


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
