"""
Health check and agent availability detection.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Dict

from fastapi import APIRouter
from sqlalchemy import text

from app.config import get_settings

logger = logging.getLogger("jarvis.api.health")
router = APIRouter()


@router.get("")
async def health_check():
    """Comprehensive health check."""
    settings = get_settings()
    checks: Dict[str, dict] = {}

    # Database
    try:
        from app.db.database import get_session
        async with get_session() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = {"status": "ok"}
    except Exception as e:
        checks["database"] = {"status": "error", "error": str(e)}

    # Redis
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url)
        await r.ping()
        checks["redis"] = {"status": "ok"}
        await r.close()
    except Exception as e:
        checks["redis"] = {"status": "unavailable", "error": str(e), "note": "Fallback to in-process tasks"}

    # Agents
    agents = await _detect_agents()
    checks["agents"] = agents

    # Rules
    from app.services.rule_engine import RuleEngine
    engine = RuleEngine()
    checks["rules"] = {
        "status": "ok",
        "count": len(engine.list_rules()),
        "rules": [r.meta.id for r in engine.list_rules()],
    }

    all_ok = all(
        c.get("status") in ("ok", "unavailable") for c in checks.values()
    )

    return {
        "status": "healthy" if all_ok else "degraded",
        "service": "jarvis",
        "checks": checks,
    }


@router.get("/agents")
async def check_agents():
    """Check which agent CLIs are available."""
    return await _detect_agents()


async def _detect_agents() -> Dict[str, dict]:
    """Detect which agent CLIs are installed and available."""
    results = {}

    # Claude Code
    results["claude_code"] = await _check_cli("claude", ["claude", "--version"])

    # Codex
    results["codex"] = await _check_cli("codex", ["codex", "--version"])

    return results


async def _check_cli(name: str, version_cmd: list) -> dict:
    """Check if a CLI tool is available."""
    # Quick check: is it in PATH?
    if not shutil.which(version_cmd[0]):
        return {
            "status": "not_installed",
            "available": False,
            "error": f"'{version_cmd[0]}' not found in PATH",
        }

    try:
        proc = await asyncio.create_subprocess_exec(
            *version_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        version = stdout.decode().strip() or stderr.decode().strip()
        return {
            "status": "ok",
            "available": True,
            "version": version[:100],
        }
    except asyncio.TimeoutError:
        return {"status": "timeout", "available": False, "error": "Version check timed out"}
    except Exception as e:
        return {"status": "error", "available": False, "error": str(e)}
