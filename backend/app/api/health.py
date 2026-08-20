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

    # Database — 之前这里的 `"SELECT 1" if False else None` 是个 bug：永远传 None
    # 给 execute()，真出错也被下面的 except 悄悄吞成 "status": "ok"，2026-08-20
    # 那次数据库真损坏期间这个检查全程显示 ok，完全没用。改成真的探一次活库。
    try:
        from sqlalchemy import text as _sql_text
        from app.db.database import get_session
        async with get_session() as session:
            await session.execute(_sql_text("SELECT 1"))
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

    # Agents —— 这是个嵌套字典（claude_code/codex 各有自己的 status），本身没有
    # 顶层 status 字段。之前直接 checks["agents"] = agents 会让下面 all_ok 的
    # `.get("status")` 永远拿 None，判成 degraded——跟 agent 实际是否可用无关，
    # 纯粹是聚合逻辑的 bug（2026-08-20 发现）。这里补一个聚合出来的顶层 status。
    agents = await _detect_agents()
    agents_ok = all(a.get("status") in ("ok", "unavailable") for a in agents.values())
    checks["agents"] = {"status": "ok" if agents_ok else "degraded", **agents}

    # Rules
    from app.services.rule_engine import RuleEngine
    engine = RuleEngine()
    checks["rules"] = {
        "status": "ok",
        "count": len(engine.list_rules()),
        "rules": [r.meta.id for r in engine.list_rules()],
    }

    # SQLite 健康监控快照（2026-08-20，纯读缓存，不触发新检查——见
    # app/services/db_health_monitor.py 顶部注释）。还没跑过第一轮检查时
    # ok=None，不计入 all_ok 判定（避免刚启动就被判成 degraded）。
    from app.services.db_health_state import get_snapshot
    snap = get_snapshot()
    db_health_status = "ok"
    if snap.integrity.ok is False or snap.journal_mode.ok is False:
        db_health_status = "error"
    checks["db_health"] = {
        "status": db_health_status,
        "integrity_check": {
            "ok": snap.integrity.ok,
            "checked_at": snap.integrity.checked_at,
            "detail": snap.integrity.detail,
        },
        "journal_mode": {
            "ok": snap.journal_mode.ok,
            "checked_at": snap.journal_mode.checked_at,
            "detail": snap.journal_mode.detail,
        },
        "recent_io_errors_10min": snap.recent_io_errors_10min,
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
