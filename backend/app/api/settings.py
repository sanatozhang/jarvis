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


# ---------------------------------------------------------------------------
# L1.5 Context Condensation settings
# ---------------------------------------------------------------------------

CONDENSATION_CONFIG_KEY = "condensation_config"

# Default models per provider
_DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash-preview-05-20",
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4.1-mini",
}

_CONDENSATION_DEFAULTS = {
    "enabled": False,
    "provider": "anthropic",
    "model": "",
    "api_key": "",
    "log_size_threshold_mb": 5,
    "time_window_hours_before": 4,
    "time_window_hours_after": 2,
    "timeout": 120,
}


@router.get("/condensation")
async def get_condensation_config():
    """Get L1.5 context condensation configuration."""
    raw = await db.get_oncall_config(CONDENSATION_CONFIG_KEY, "")
    if raw:
        config = json.loads(raw)
    else:
        config = dict(_CONDENSATION_DEFAULTS)

    # Fill any missing keys with defaults
    for k, v in _CONDENSATION_DEFAULTS.items():
        config.setdefault(k, v)

    # Mask API key for frontend display
    if config.get("api_key"):
        key = config["api_key"]
        config["api_key_masked"] = key[:8] + "••••" + key[-4:] if len(key) > 12 else "••••"
        config["has_api_key"] = True
    else:
        config["api_key_masked"] = ""
        config["has_api_key"] = False

    # Don't send raw API key to frontend
    config.pop("api_key", None)

    # Attach default model hints
    config["default_models"] = _DEFAULT_MODELS

    return config


class CondensationConfigUpdate(BaseModel):
    enabled: bool | None = None
    provider: str | None = None
    model: str | None = None
    api_key: str | None = None  # empty string = keep existing
    log_size_threshold_mb: float | None = None
    time_window_hours_before: int | None = None
    time_window_hours_after: int | None = None
    timeout: int | None = None


@router.put("/condensation")
async def update_condensation_config(req: CondensationConfigUpdate):
    """Update L1.5 context condensation configuration."""
    # Load existing config
    raw = await db.get_oncall_config(CONDENSATION_CONFIG_KEY, "")
    config = json.loads(raw) if raw else dict(_CONDENSATION_DEFAULTS)

    # Update only provided fields
    if req.enabled is not None:
        config["enabled"] = req.enabled
    if req.provider is not None:
        config["provider"] = req.provider
    if req.model is not None:
        config["model"] = req.model
    if req.api_key is not None and req.api_key != "":
        # Only update API key if a new value is provided (not empty)
        config["api_key"] = req.api_key
    if req.log_size_threshold_mb is not None:
        config["log_size_threshold_mb"] = req.log_size_threshold_mb
    if req.time_window_hours_before is not None:
        config["time_window_hours_before"] = req.time_window_hours_before
    if req.time_window_hours_after is not None:
        config["time_window_hours_after"] = req.time_window_hours_after
    if req.timeout is not None:
        config["timeout"] = req.timeout

    await db.set_oncall_config(CONDENSATION_CONFIG_KEY, json.dumps(config, ensure_ascii=False))
    logger.info("Updated condensation config: enabled=%s, provider=%s", config.get("enabled"), config.get("provider"))

    return {"status": "updated"}
