"""
jarvis - Plaud 崩溃自动化平台
FastAPI application entry point.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# 把项目根 .env 落进 os.environ，让 os.environ.get(...) 类代码（GH_TOKEN 等）拿得到。
# pydantic-settings 的 env_file 只塞进 Settings 实例，不写 os.environ。
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings, SSOSettings
from app.db.database import init_db, close_db

logger = logging.getLogger("jarvis")


def _validate_sso_startup(sso: "SSOSettings") -> None:
    """Fail-fast: refuse to start if SSO is enabled but config is broken."""
    if not sso.enabled:
        return
    if not sso.feishu_app_id:
        raise RuntimeError("ENABLE_SSO=true requires SSO_FEISHU_APP_ID")
    if not sso.feishu_app_secret:
        raise RuntimeError("ENABLE_SSO=true requires SSO_FEISHU_APP_SECRET")
    if not sso.jwt_secret or len(sso.jwt_secret) < 32:
        raise RuntimeError("ENABLE_SSO=true requires SSO_JWT_SECRET (>= 32 chars)")
    # NOTE: removed https-only check for redirect_uri because Feishu accepts
    # http://localhost during dev; cookie_secure flag separately gates HTTPS
    # cookie behavior in production.
    if not sso.admin_emails:
        logger.warning("ADMIN_EMAILS empty — no admin will be created via SSO")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup / shutdown."""
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Starting jarvis...")

    _validate_sso_startup(settings.sso)

    # Import crashguard models to register with SQLAlchemy Base
    from app.crashguard import models as _crashguard_models  # noqa: F401
    # Import coreguard models too (independent module, same Base)
    from app.coreguard import models as _coreguard_models  # noqa: F401
    # Import platform_tickets models too (same Base; module kept only because
    # db/database.py's UNION queries reference PlatformTicket — not used by
    # crashguard/coreguard/graygate themselves)
    from app.platform_tickets import models as _platform_tickets_models  # noqa: F401

    await init_db()
    logger.info("Database initialized.")

    # Apply DB-persisted agent runtime overrides (call_mode, api_traffic_ratio 等)
    try:
        from app.api.settings import apply_agent_overrides_from_db
        await apply_agent_overrides_from_db()
    except Exception as e:
        logger.warning("apply_agent_overrides_from_db failed (non-fatal): %s", e)

    try:
        from app.api.settings import apply_repo_routing_overrides_from_db
        await apply_repo_routing_overrides_from_db()
    except Exception as e:
        logger.warning("apply_repo_routing_overrides_from_db failed (non-fatal): %s", e)

    # DB 解耦自检（crash_* + pt_* 两个独立前缀域）— 违规则阻止启动
    try:
        from scripts.check_crash_decoupling import assert_crash_tables_decoupled
        assert_crash_tables_decoupled(("crash_", "pt_"))
        logger.info("Crash/platform-ticket decoupling check passed.")
    except RuntimeError as e:
        logger.error("Crash/platform-ticket decoupling check FAILED: %s", e)
        raise

    # Crashguard 轻量自动迁移 — SQLite 已建表后追加新列
    try:
        from app.crashguard.migrations import ensure_columns
        await ensure_columns()
    except Exception as e:
        logger.warning("Crashguard auto-migration skipped: %s", e)

    # SQLite 健康监控（I/O 错误频率 / 定期 integrity_check / journal_mode 漂移哨兵）
    db_health_task = None
    if getattr(settings, "db_health_monitor_enabled", True):
        from app.services.db_health_monitor import db_health_monitor_loop
        db_health_task = asyncio.create_task(db_health_monitor_loop())

    # Start daily code repo updater (pulls main branch between 2-6 AM) — keeps
    # the bind-mounted source checkouts fresh for crashguard's own code reads
    # (symbol resolution / PR drafting) and for repo_router path resolution.
    from app.services.repo_updater import repo_update_loop
    repo_update_task = asyncio.create_task(repo_update_loop())

    # Release build status poller (Jenkins) — only spins if jenkins.enabled
    from app.workers.release_poller import release_poller_loop
    release_poller_task = asyncio.create_task(release_poller_loop())

    # Crashguard 早晚报调度（每 60 秒 tick；命中 morning/evening cron 即推飞书）
    from app.crashguard.workers.scheduler import report_scheduler_loop
    crashguard_scheduler_task = asyncio.create_task(report_scheduler_loop())

    # Crashguard 启动预热 + 周期 pipeline（与早晚报解耦，重启后 60s 自动跑一次）
    from app.crashguard.config import get_crashguard_settings as _cg_settings
    from app.crashguard.workers.warmup import warmup_on_startup, pipeline_scheduler_loop
    _cg = _cg_settings()
    crashguard_warmup_task = None
    if _cg.enabled and getattr(_cg, "warmup_on_startup", True):
        crashguard_warmup_task = asyncio.create_task(warmup_on_startup())
        logger.info("crashguard warmup scheduled (60s after startup)")
    crashguard_pipeline_task = asyncio.create_task(pipeline_scheduler_loop())

    # Coreguard 每小时 :15 SHoW 对比 22 指标（独立子模块，无 import 耦合）
    # 治本：FastAPI 0.93+ lifespan 与 @app.on_event("startup") 互斥，老 on_event 装饰器
    # 被静默忽略。必须挂到 lifespan 内才会真启动（2026-05-26 修复）。
    coreguard_scheduler_task = None
    try:
        from app.coreguard.config import get_coreguard_settings
        _cog = get_coreguard_settings()
        if _cog.enabled and _cog.scheduler_enabled:
            from app.coreguard.workers.scheduler import scheduler_loop as _coreguard_loop
            coreguard_scheduler_task = asyncio.create_task(_coreguard_loop())
            logger.info("coreguard scheduler started (cron=%s)", _cog.hourly_watch_cron)
        else:
            logger.info("coreguard scheduler disabled (enabled=%s scheduler_enabled=%s)",
                        _cog.enabled, _cog.scheduler_enabled)
    except Exception as e:
        logger.warning("coreguard scheduler start failed (non-fatal): %s", e)

    # Graygate 4.0.3 灰度期临时监控（每天 09:00 Asia/Shanghai 发日报；独立子模块，无 import 耦合）
    graygate_scheduler_task = None
    try:
        from app.graygate.config import get_graygate_settings
        _gg = get_graygate_settings()
        if _gg.enabled and _gg.scheduler_enabled:
            from app.graygate.workers.scheduler import scheduler_loop as _graygate_loop
            graygate_scheduler_task = asyncio.create_task(_graygate_loop())
            logger.info("graygate scheduler started (report_hour_bjt=%d)", _gg.report_hour_bjt)
        else:
            logger.info("graygate scheduler disabled (enabled=%s scheduler_enabled=%s)",
                        _gg.enabled, _gg.scheduler_enabled)
    except Exception as e:
        logger.warning("graygate scheduler start failed (non-fatal): %s", e)

    yield

    if crashguard_warmup_task is not None:
        crashguard_warmup_task.cancel()
    crashguard_pipeline_task.cancel()
    crashguard_scheduler_task.cancel()
    if coreguard_scheduler_task is not None:
        coreguard_scheduler_task.cancel()
    if graygate_scheduler_task is not None:
        graygate_scheduler_task.cancel()
    release_poller_task.cancel()
    repo_update_task.cancel()
    if db_health_task is not None:
        db_health_task.cancel()
    await close_db()
    logger.info("jarvis stopped.")


app = FastAPI(
    title="jarvis",
    description="Plaud 崩溃自动化平台",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS - allow frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from app.middleware.auth import AuthMiddleware

# Use live settings lookup so test overrides (conftest mutating
# get_settings().sso.enabled) take effect without rebuilding the middleware.
app.add_middleware(
    AuthMiddleware,
    settings_getter=lambda: get_settings().sso,
)

# ---------------------------------------------------------------------------
# Register API routers
# ---------------------------------------------------------------------------
from app.api.settings import router as settings_router
from app.api.health import router as health_router
from app.api.site_feedback import router as site_feedback_router
from app.api.users import router as users_router
from app.api.auth import router as auth_router
from app.api.env_settings import router as env_settings_router
from app.api.release import router as release_router

app.include_router(settings_router, prefix="/api/settings", tags=["Settings"])
app.include_router(health_router, prefix="/api/health", tags=["Health"])
app.include_router(site_feedback_router, prefix="/api/site-feedback", tags=["SiteFeedback"])
app.include_router(auth_router, prefix="/api/auth", tags=["Auth"])
app.include_router(users_router, prefix="/api/users", tags=["Users"])
app.include_router(env_settings_router, prefix="/api/env", tags=["Env Settings"])
app.include_router(release_router, prefix="/api/release", tags=["Release"])

# Crashguard API（独立子模块，prefix 在 router 内部声明 /api/crash）
from app.crashguard.api import crash as _crash_api  # noqa: E402
app.include_router(_crash_api.router)

# Coreguard API（独立子模块，prefix /api/coreguard，demo 阶段）
from app.coreguard.api import coreguard as _coreguard_api  # noqa: E402
app.include_router(_coreguard_api.router)

# Graygate API（独立子模块，prefix /api/graygate，4.0.3 灰度期临时功能）
from app.graygate.api.graygate import router as _graygate_api_router  # noqa: E402
app.include_router(_graygate_api_router)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
        log_level=settings.log_level,
    )
