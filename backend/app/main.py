"""
Appllo - Plaud 工单智能分析平台
FastAPI application entry point.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db.database import init_db, close_db

logger = logging.getLogger("appllo")

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
    await init_db()
    logger.info("Database initialized.")

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

    # Start periodic zombie task cleanup
    zombie_task = asyncio.create_task(_zombie_cleanup_loop())

    # Start daily code repo updater (pulls main branch between 2-6 AM)
    from app.services.repo_updater import repo_update_loop
    repo_update_task = asyncio.create_task(repo_update_loop())

    # Daily escalation reminder (09:00 Asia/Shanghai) — gated by ENABLE_ONCALL_NOTIFY
    import os
    reminder_task = None
    if os.environ.get("ENABLE_ONCALL_NOTIFY", "false").lower() == "true":
        from app.services.escalation_reminder import escalation_reminder_loop
        reminder_task = asyncio.create_task(escalation_reminder_loop())
        logger.info("Escalation reminder loop started (ENABLE_ONCALL_NOTIFY=true)")
    else:
        logger.info("Escalation reminder disabled (set ENABLE_ONCALL_NOTIFY=true to enable)")

    yield

    if reminder_task is not None:
        reminder_task.cancel()
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
from app.api.feedback import router as feedback_router
from app.api.users import router as users_router
from app.api.oncall import router as oncall_router
from app.api.v1_analyze import router as v1_analyze_router
from app.api.env_settings import router as env_settings_router
from app.api.analytics import router as analytics_router
from app.api.linear_webhook import router as linear_webhook_router
from app.api.golden_samples import router as golden_samples_router
from app.api.eval import router as eval_router
from app.api.tools import router as tools_router
from app.api.wishes import router as wishes_router

app.include_router(issues_router, prefix="/api/issues", tags=["Issues"])
app.include_router(tasks_router, prefix="/api/tasks", tags=["Tasks"])
app.include_router(rules_router, prefix="/api/rules", tags=["Rules"])
app.include_router(settings_router, prefix="/api/settings", tags=["Settings"])
app.include_router(reports_router, prefix="/api/reports", tags=["Reports"])
app.include_router(health_router, prefix="/api/health", tags=["Health"])
app.include_router(local_router, prefix="/api/local", tags=["Local"])
app.include_router(feedback_router, prefix="/api/feedback", tags=["Feedback"])
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
