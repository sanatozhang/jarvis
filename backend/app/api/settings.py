"""
API routes for system settings / agent configuration.
"""

from __future__ import annotations

import json
import logging
from typing import List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.db import database as db
from app.models.schemas import AgentConfigUpdate

logger = logging.getLogger("jarvis.api.settings")
router = APIRouter()

# ---------------------------------------------------------------------------
# Agent runtime overrides — DB-persisted（治本：UI 切换 call_mode 后跨重启生效）
# ---------------------------------------------------------------------------
# 抓手：把 PUT /agent 的字段写入 oncall_config 表，启动时 apply_agent_overrides_from_db
# 把 DB 值 merge 回内存 Settings。覆盖优先级：env > DB override > yaml > defaults。
# 不直接改 yaml：yaml 是模板（含注释），改 yaml 会破坏可读性；DB override 更轻量、可审计。
AGENT_OVERRIDE_KEY = "agent_runtime_overrides"


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
        "call_mode": ag.call_mode,
        "api_traffic_ratio": ag.api_traffic_ratio,
        "timeout": ag.timeout,
        "max_turns": ag.max_turns,
        "providers": providers,
        "routing": ag.routing,
    }


@router.put("/agent")
async def update_agent_config(req: AgentConfigUpdate):
    """Update agent configuration — runtime memory + DB-persisted override.

    底层逻辑：写两份。内存里改 settings 让本次请求即时生效；DB 里写一份
    override，下次启动 apply_agent_overrides_from_db 会再 merge 回来——
    跨重启不再退回 yaml 默认（治本 bug：fb_f57ddda7d0 现象的根因）。
    """
    settings = get_settings()

    # Load existing override（避免覆盖未提交字段）
    raw = await db.get_oncall_config(AGENT_OVERRIDE_KEY, "")
    override = json.loads(raw) if raw else {}

    if req.default_agent is not None:
        settings.agent.default = req.default_agent
        override["default"] = req.default_agent
    if req.call_mode is not None:
        mode = req.call_mode.strip().lower()
        if mode not in ("api", "cli"):
            raise HTTPException(status_code=400, detail="call_mode must be 'api' or 'cli'")
        settings.agent.call_mode = mode
        override["call_mode"] = mode
    if req.api_traffic_ratio is not None:
        ratio = float(req.api_traffic_ratio)
        if not (0.0 <= ratio <= 1.0):
            raise HTTPException(status_code=400, detail="api_traffic_ratio must be between 0.0 and 1.0")
        settings.agent.api_traffic_ratio = ratio
        override["api_traffic_ratio"] = ratio
    if req.timeout is not None:
        settings.agent.timeout = req.timeout
        override["timeout"] = req.timeout
    if req.max_turns is not None:
        settings.agent.max_turns = req.max_turns
        override["max_turns"] = req.max_turns
    if req.routing is not None:
        settings.agent.routing.update(req.routing)
        # routing 是字典，存合并后的全量
        override["routing"] = dict(settings.agent.routing)

    # 持久化到 DB（与 condensation 模块用同一张 oncall_config 表）
    await db.set_oncall_config(AGENT_OVERRIDE_KEY, json.dumps(override, ensure_ascii=False))
    logger.info(
        "Agent config updated + persisted: keys=%s (call_mode=%s, default=%s)",
        list(override.keys()), settings.agent.call_mode, settings.agent.default,
    )

    return {
        "status": "updated",
        "agent": settings.agent.default,
        "call_mode": settings.agent.call_mode,
        "api_traffic_ratio": settings.agent.api_traffic_ratio,
        "persisted_keys": list(override.keys()),
    }


async def apply_agent_overrides_from_db() -> dict:
    """Startup hook: load agent_runtime_overrides from DB and merge into in-memory settings.

    Called from main.py lifespan after init_db(). Idempotent.
    优先级：env (config.py 已处理) > DB override (本函数) > yaml > defaults。
    返回 applied dict 供日志/审计；空 dict 表示 DB 无 override。
    """
    try:
        raw = await db.get_oncall_config(AGENT_OVERRIDE_KEY, "")
        if not raw:
            return {}
        override = json.loads(raw)
    except Exception as e:
        logger.warning("Failed to load agent override from DB (non-fatal): %s", e)
        return {}

    settings = get_settings()
    applied = {}
    # env 优先：若用户已设 AGENT_DEFAULT / AGENT_CALL_MODE，则 DB override 不再覆盖
    import os as _os
    if "default" in override and not _os.getenv("AGENT_DEFAULT"):
        settings.agent.default = override["default"]
        applied["default"] = override["default"]
    if "call_mode" in override and not _os.getenv("AGENT_CALL_MODE"):
        settings.agent.call_mode = override["call_mode"]
        applied["call_mode"] = override["call_mode"]
    if "api_traffic_ratio" in override:
        settings.agent.api_traffic_ratio = float(override["api_traffic_ratio"])
        applied["api_traffic_ratio"] = override["api_traffic_ratio"]
    if "timeout" in override:
        settings.agent.timeout = int(override["timeout"])
        applied["timeout"] = override["timeout"]
    if "max_turns" in override:
        settings.agent.max_turns = int(override["max_turns"])
        applied["max_turns"] = override["max_turns"]
    if "routing" in override and isinstance(override["routing"], dict):
        settings.agent.routing.update(override["routing"])
        applied["routing"] = override["routing"]

    if applied:
        logger.info("Agent overrides applied from DB: %s", applied)
    return applied


# ---------------------------------------------------------------------------
# Repo-routing overrides — DB-persisted（UI 配置 repo_routing + service_filter）
# ---------------------------------------------------------------------------
REPO_ROUTING_OVERRIDE_KEY = "repo_routing_overrides"

from app.config import get_repo_routing  # noqa: E402  (module-level for monkeypatch)


class RepoRoutingUpdate(BaseModel):
    routing: dict
    service_filter: str | None = None
    support_web: bool | None = None
    support_desktop: bool | None = None


class PreviewReq(BaseModel):
    platform: str
    version: str | None = None


@router.get("/repo-routing")
async def get_repo_routing_cfg():
    """Get current repo-routing config + crashguard service_filter."""
    from app.crashguard.config import get_crashguard_settings
    s = get_settings()
    return {
        "routing": get_repo_routing(),
        "service_filter": get_crashguard_settings().datadog_service_filter,
        "support_web": s.support_web,
        "support_desktop": s.support_desktop,
    }


def _validate_routing(routing: dict) -> list[dict]:
    """Validate each band in a routing config and return a list of warnings.

    Checks per band:
    - wrapper path exists on disk
    - wrapper has a .git entry (is a git repo or submodule shell)
    - sub path exists inside wrapper (if sub is non-empty)

    Does NOT raise; always returns a (possibly empty) list so callers can
    choose to surface warnings without blocking the write.
    """
    import os as _os
    warnings: list[dict] = []
    for platform, cfg in (routing or {}).items():
        for i, band in enumerate(cfg.get("bands", [])):
            wrapper = _os.path.expanduser((band.get("wrapper") or "").strip())
            sub = (band.get("sub") or "").strip()
            if not wrapper:
                continue
            if not _os.path.exists(wrapper):
                warnings.append({
                    "platform": platform,
                    "band": i,
                    "issue": f"wrapper not found: {wrapper}",
                })
                continue
            if not _os.path.exists(_os.path.join(wrapper, ".git")):
                warnings.append({
                    "platform": platform,
                    "band": i,
                    "issue": f"wrapper exists but no .git entry (not a git repo/submodule): {wrapper}",
                })
            if sub:
                sub_path = _os.path.join(wrapper, sub)
                if not _os.path.exists(sub_path):
                    warnings.append({
                        "platform": platform,
                        "band": i,
                        "issue": f"sub not found inside wrapper: {sub_path}",
                    })
    return warnings


@router.put("/repo-routing")
async def update_repo_routing(req: RepoRoutingUpdate):
    """Write repo-routing override to DB and apply immediately into memory."""
    override: dict = {"routing": req.routing}
    if req.service_filter is not None:
        override["service_filter"] = req.service_filter
    if req.support_web is not None:
        override["support_web"] = req.support_web
    if req.support_desktop is not None:
        override["support_desktop"] = req.support_desktop
    await db.set_oncall_config(REPO_ROUTING_OVERRIDE_KEY, json.dumps(override, ensure_ascii=False))
    _apply_repo_routing(override)
    logger.info("Repo-routing override persisted + applied: routing keys=%s", list(req.routing.keys()))
    warnings = _validate_routing(req.routing)
    if warnings:
        logger.warning("Repo-routing PUT validation warnings: %s", warnings)
    return {"ok": True, "warnings": warnings}


@router.post("/repo-routing/preview")
async def preview_repo_routing(req: PreviewReq):
    """Resolve a (platform, version) pair against current routing config."""
    from app.services import repo_router
    res = repo_router.resolve(req.platform, req.version, get_repo_routing())
    if not res:
        return {"resolved": False, "reason": "platform 未配置 / 路径不存在 / 版本无法归一"}
    return {
        "resolved": True,
        "family": res.family,
        "platform": res.platform,
        "sub_repo_path": res.sub_repo_path,
        "github_repo": res.github_repo,
        "symbol_profile": res.symbol_profile,
        "confidence": res.confidence,
    }


def _apply_repo_routing(override: dict) -> None:
    """Merge override dict into in-memory Settings.repo_routing + crashguard service_filter."""
    s = get_settings()
    if "routing" in override:
        s.repo_routing = override["routing"]
    if "service_filter" in override:
        from app.crashguard.config import get_crashguard_settings
        get_crashguard_settings().datadog_service_filter = override["service_filter"]
    if "support_web" in override:
        s.support_web = bool(override["support_web"])
    if "support_desktop" in override:
        s.support_desktop = bool(override["support_desktop"])


async def apply_repo_routing_overrides_from_db() -> dict:
    """Startup hook: load repo_routing_overrides from DB and merge into in-memory settings.

    Called from main.py lifespan after init_db(). Idempotent.
    Returns applied dict for logging; empty dict means no override in DB.
    """
    try:
        raw = await db.get_oncall_config(REPO_ROUTING_OVERRIDE_KEY, "")
        if not raw:
            return {}
        override = json.loads(raw)
    except Exception as e:
        logger.warning("Failed to load repo_routing override from DB (non-fatal): %s", e)
        return {}
    _apply_repo_routing(override)
    if override:
        logger.info("Repo-routing overrides applied from DB: keys=%s", list(override.keys()))
    return override


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
