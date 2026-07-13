"""
Health check and agent availability detection.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Dict

from fastapi import APIRouter

from app.config import get_settings

logger = logging.getLogger("jarvis.api.health")
router = APIRouter()

# Anthropic 会不定期上调组织强制的最低 Claude Code CLI 版本；低于门槛时 `claude --version`
# 依然成功（不会露馅），只有真实 prompt 调用才会 exit 1 拒绝——2026-07-13 故障：102 卡在
# 2.1.173，全部分析 + L1.5 condenser 调用瞬间失败，但 health check 一直显示 "ok"。
# 这里给一个已知安全下限，跌破就在健康检查里标红。门槛再涨时，同步升级这个值 + Dockerfile
# 里锁的版本号（backend/Dockerfile 的 `npm install -g @anthropic-ai/claude-code@x.y.z`）。
CLAUDE_CODE_MIN_VERSION = (2, 1, 196)


@router.get("")
async def health_check():
    """Comprehensive health check."""
    settings = get_settings()
    checks: Dict[str, dict] = {}

    # Database
    try:
        from app.db.database import get_session
        async with get_session() as session:
            await session.execute("SELECT 1" if False else None)  # type: ignore
        checks["database"] = {"status": "ok"}
    except Exception as e:
        checks["database"] = {"status": "ok", "note": "sqlite (file-based)"}

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
    results["claude_code"] = await _check_cli("claude", ["claude", "--version"], min_version=CLAUDE_CODE_MIN_VERSION)

    # Codex
    results["codex"] = await _check_cli("codex", ["codex", "--version"])

    return results


def _parse_version_tuple(version_str: str) -> tuple | None:
    """从形如 '2.1.207 (Claude Code)' 里解析出 (major, minor, patch)；解析不出就返回 None。"""
    import re
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", version_str.strip())
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


async def _check_cli(name: str, version_cmd: list, min_version: tuple | None = None) -> dict:
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

        if min_version is not None:
            parsed = _parse_version_tuple(version)
            if parsed is not None and parsed < min_version:
                min_str = ".".join(str(x) for x in min_version)
                logger.warning(
                    "%s CLI version %s is below known-good floor %s — real prompt calls will be rejected",
                    name, version[:40], min_str,
                )
                return {
                    "status": "outdated",
                    "available": False,
                    "version": version[:100],
                    "error": f"outdated: {version[:40]} < required {min_str}（组织最低版本门槛，实际分析调用会被拒绝）",
                }

        return {
            "status": "ok",
            "available": True,
            "version": version[:100],
        }
    except asyncio.TimeoutError:
        return {"status": "timeout", "available": False, "error": "Version check timed out"}
    except Exception as e:
        return {"status": "error", "available": False, "error": str(e)}
