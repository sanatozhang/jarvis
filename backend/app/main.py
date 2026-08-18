"""
Appllo - Plaud 工单智能分析平台
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

logger = logging.getLogger("appllo")


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

# Max time (seconds) a task can stay in analyzing/downloading/etc before considered zombie
_ZOMBIE_TIMEOUT_SEC = 30 * 60  # 30 minutes


async def _zombie_cleanup_loop():
    """Periodically mark tasks stuck in active states as failed.

    This handles cases where DB writes fail (e.g. disk full) and the task
    status never gets updated, leaving tasks permanently in 'analyzing'.
    """
    from app.db.database import get_session
    from sqlalchemy import text

    while True:
        await asyncio.sleep(300)  # check every 5 minutes
        try:
            async with get_session() as s:
                # Mark tasks that have been in active states for too long
                r1 = await s.execute(text(
                    "UPDATE tasks SET status='failed', "
                    "error='任务超时，可能因磁盘空间不足或其他外部原因导致' "
                    "WHERE status IN ('analyzing','queued','downloading','decrypting','extracting') "
                    f"AND updated_at < datetime('now', '-{_ZOMBIE_TIMEOUT_SEC} seconds')"
                ))
                r2 = await s.execute(text(
                    "UPDATE issues SET status='failed' "
                    "WHERE status='analyzing' "
                    "AND id IN (SELECT issue_id FROM tasks WHERE status='failed' "
                    f"AND updated_at < datetime('now', '-{_ZOMBIE_TIMEOUT_SEC} seconds'))"
                ))
                await s.commit()
                if r1.rowcount or r2.rowcount:
                    logger.warning(
                        "Zombie cleanup: marked %d tasks and %d issues as failed",
                        r1.rowcount, r2.rowcount,
                    )
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("Periodic zombie cleanup failed (will retry): %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup / shutdown."""
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Starting Appllo...")

    _validate_sso_startup(settings.sso)

    # Import crashguard models to register with SQLAlchemy Base
    from app.crashguard import models as _crashguard_models  # noqa: F401
    # Import coreguard models too (independent module, same Base)
    from app.coreguard import models as _coreguard_models  # noqa: F401
    # Import platform_tickets models too (new-platform ticket storage, same Base)
    from app.platform_tickets import models as _platform_tickets_models  # noqa: F401

    await init_db()
    logger.info("Database initialized.")

    # Apply DB-persisted agent runtime overrides (call_mode, api_traffic_ratio 等)
    # 治本：UI 切换 call_mode 后跨重启生效，不再被 yaml 默认值偷偷覆盖。
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
        assert_crash_tables_decoupled()
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

    # Platform tickets 轻量自动迁移骨架 — 当前 _REQUIRED_COLUMNS 为空，no-op；
    # 保留调用位置供未来给 pt_tickets 加列时直接生效，无需改这里。
    try:
        from app.platform_tickets.migrations import ensure_columns as _pt_ensure_columns
        await _pt_ensure_columns()
    except Exception as e:
        logger.warning("Platform tickets auto-migration skipped: %s", e)

    # Clean up zombie tasks from previous crashes/restarts
    from app.db.database import get_session
    from sqlalchemy import text
    try:
        async with get_session() as s:
            r1 = await s.execute(text(
                "UPDATE tasks SET status='failed', error='服务器重启，任务中断' "
                "WHERE status IN ('analyzing','queued','downloading','decrypting','extracting')"
            ))
            r2 = await s.execute(text(
                "UPDATE issues SET status='failed' WHERE status='analyzing'"
            ))
            await s.commit()
            if r1.rowcount or r2.rowcount:
                logger.warning("Cleaned up %d zombie tasks, %d zombie issues", r1.rowcount, r2.rowcount)
    except Exception as e:
        logger.warning("Zombie cleanup failed (non-fatal): %s", e)

    # Sync file-based rules to DB
    try:
        from app.services.rule_engine import RuleEngine
        engine = RuleEngine()
        await engine.sync_files_to_db()
        logger.info("Rules synced to DB: %d total", len(engine.list_rules()))
    except Exception as e:
        logger.warning("Rule sync failed (non-fatal): %s", e)

    # VOC Portal taxonomy — bootstrap from checked-in seed (no-op once DB has data),
    # then load the active-tags cache. Live daily sync only starts if voc.sync_enabled
    # (default False until a VOC service account is provisioned).
    try:
        from app.services import voc_taxonomy
        seed_result = await voc_taxonomy.sync_seed_to_db()
        await voc_taxonomy.reload_from_db()
        if not seed_result["skipped"]:
            logger.info("VOC taxonomy bootstrapped from seed: %d tags", len(seed_result["added"]))
    except Exception as e:
        logger.warning("VOC taxonomy bootstrap failed (non-fatal): %s", e)

    voc_sync_task = None
    if settings.voc.sync_enabled:
        from app.services.voc_taxonomy import voc_sync_loop
        voc_sync_task = asyncio.create_task(voc_sync_loop())
        logger.info("VOC taxonomy daily sync loop started (every %dh)", settings.voc.sync_interval_hours)
    else:
        logger.info("VOC taxonomy sync disabled (voc.sync_enabled=false) — using DB/seed snapshot only")

    from app.services.voc_digest import voc_digest_loop
    voc_digest_task = asyncio.create_task(voc_digest_loop())

    # Start periodic zombie task cleanup
    zombie_task = asyncio.create_task(_zombie_cleanup_loop())

    # Start daily code repo updater (pulls main branch between 2-6 AM)
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

    # Daily escalation reminder (09:00 Asia/Shanghai) — gated by ENABLE_ONCALL_NOTIFY
    import os
    reminder_task = None
    if os.environ.get("ENABLE_ONCALL_NOTIFY", "false").lower() == "true":
        from app.services.escalation_reminder import escalation_reminder_loop
        reminder_task = asyncio.create_task(escalation_reminder_loop())
        logger.info("Escalation reminder loop started (ENABLE_ONCALL_NOTIFY=true)")
    else:
        logger.info("Escalation reminder disabled (set ENABLE_ONCALL_NOTIFY=true to enable)")

    # Weekly oncall sync FROM Feishu「本周值班」表 (已下线 2026-08-18)
    # 排班真相源已改为 Jarvis 平台自身排班。保留代码供需要临时恢复时使用，设 ENABLE_ONCALL_FEISHU_SYNC=true。
    oncall_feishu_sync_task = None
    if os.environ.get("ENABLE_ONCALL_FEISHU_SYNC", "false").lower() == "true":
        from app.services.oncall_feishu_sync import oncall_feishu_sync_loop
        oncall_feishu_sync_task = asyncio.create_task(oncall_feishu_sync_loop())
        logger.info("Oncall Feishu sync loop started (ENABLE_ONCALL_FEISHU_SYNC=true)")
    else:
        logger.info("Oncall Feishu sync disabled (set ENABLE_ONCALL_FEISHU_SYNC=true to enable)")

    # Weekly oncall greeting message to Feishu group (每周一 09:00 Asia/Shanghai)
    # 永久默认 false，只在生产服务器 .env 里显式打开——不是"评审后改 true"的过渡态。
    # .env 不进 git，这是对"野实例/笔记本 clone 出来的仓库自动带着开关"最有效的防线。
    oncall_weekly_greeting_task = None
    if os.environ.get("ENABLE_ONCALL_WEEKLY_GREETING", "false").lower() == "true":
        from app.services.oncall_weekly_greeting import oncall_weekly_greeting_loop
        oncall_weekly_greeting_task = asyncio.create_task(oncall_weekly_greeting_loop())
        logger.info(
            "Oncall weekly greeting loop started (ENABLE_ONCALL_WEEKLY_GREETING=true, chat_id=%s)",
            get_settings().feishu.oncall_greeting_chat_id,
        )
    else:
        logger.info("Oncall weekly greeting disabled (set ENABLE_ONCALL_WEEKLY_GREETING=true to enable)")

    yield

    if voc_sync_task is not None:
        voc_sync_task.cancel()
    if voc_digest_task is not None:
        voc_digest_task.cancel()
    if reminder_task is not None:
        reminder_task.cancel()
    if oncall_feishu_sync_task is not None:
        oncall_feishu_sync_task.cancel()
    if oncall_weekly_greeting_task is not None:
        oncall_weekly_greeting_task.cancel()
    if crashguard_warmup_task is not None:
        crashguard_warmup_task.cancel()
    crashguard_pipeline_task.cancel()
    crashguard_scheduler_task.cancel()
    release_poller_task.cancel()
    repo_update_task.cancel()
    zombie_task.cancel()
    await close_db()
    logger.info("Appllo stopped.")


app = FastAPI(
    title="Appllo",
    description="Plaud 工单智能分析平台",
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
from app.api.issues import router as issues_router
from app.api.tasks import router as tasks_router
from app.api.rules import router as rules_router
from app.api.settings import router as settings_router
from app.api.reports import router as reports_router
from app.api.health import router as health_router
from app.api.local import router as local_router
from app.api.site_feedback import router as site_feedback_router
from app.api.feedback import router as feedback_router
from app.api.users import router as users_router
from app.api.auth import router as auth_router
from app.api.oncall import router as oncall_router
from app.api.v1_analyze import router as v1_analyze_router
from app.api.env_settings import router as env_settings_router
from app.api.analytics import router as analytics_router
from app.api.linear_webhook import router as linear_webhook_router
from app.api.golden_samples import router as golden_samples_router
from app.api.eval import router as eval_router
from app.api.tools import router as tools_router
from app.api.wishes import router as wishes_router
from app.api.release import router as release_router
from app.api.voc import router as voc_router

app.include_router(issues_router, prefix="/api/issues", tags=["Issues"])
app.include_router(tasks_router, prefix="/api/tasks", tags=["Tasks"])
app.include_router(rules_router, prefix="/api/rules", tags=["Rules"])
app.include_router(settings_router, prefix="/api/settings", tags=["Settings"])
app.include_router(reports_router, prefix="/api/reports", tags=["Reports"])
app.include_router(health_router, prefix="/api/health", tags=["Health"])
app.include_router(local_router, prefix="/api/local", tags=["Local"])
app.include_router(site_feedback_router, prefix="/api/site-feedback", tags=["SiteFeedback"])
app.include_router(feedback_router, prefix="/api/feedback", tags=["Feedback"])
app.include_router(auth_router, prefix="/api/auth", tags=["Auth"])
app.include_router(users_router, prefix="/api/users", tags=["Users"])
app.include_router(oncall_router, prefix="/api/oncall", tags=["Oncall"])
app.include_router(v1_analyze_router, prefix="/api/v1", tags=["V1 Public API"])
app.include_router(env_settings_router, prefix="/api/env", tags=["Env Settings"])
app.include_router(analytics_router, prefix="/api/analytics", tags=["Analytics"])
app.include_router(linear_webhook_router, prefix="/api/linear", tags=["Linear"])
app.include_router(golden_samples_router, prefix="/api/golden-samples", tags=["Golden Samples"])
app.include_router(eval_router, prefix="/api/eval", tags=["Eval"])
app.include_router(tools_router, prefix="/api/tools", tags=["Tools"])
app.include_router(wishes_router, prefix="/api/wishes", tags=["Wishes"])
app.include_router(release_router, prefix="/api/release", tags=["Release"])
app.include_router(voc_router, prefix="/api/voc", tags=["VOC"])

# Crashguard API（独立子模块，prefix 在 router 内部声明 /api/crash）
from app.crashguard.api import crash as _crash_api  # noqa: E402
app.include_router(_crash_api.router)

# Coreguard API（独立子模块，prefix /api/coreguard，demo 阶段）
from app.coreguard.api import coreguard as _coreguard_api  # noqa: E402
app.include_router(_coreguard_api.router)


# Coreguard scheduler 已挂到 lifespan 函数内（main.py:182+）。
# 这里之前的 @app.on_event("startup") 装饰器在 FastAPI 0.93+ 用 lifespan 后被静默忽略，
# 是 2026-05-26 早报缺失业务指标的根因——已移除，避免后续误以为还能用。


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
