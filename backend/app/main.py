"""
Jarvis - Plaud 工单智能分析平台
FastAPI application entry point.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db.database import init_db, close_db

logger = logging.getLogger("jarvis")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup / shutdown."""
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Starting Jarvis...")
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

    yield
    await close_db()
    logger.info("Jarvis stopped.")


app = FastAPI(
    title="Jarvis",
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
from app.api.linear_webhook import router as linear_webhook_router

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
app.include_router(linear_webhook_router, prefix="/api/linear", tags=["Linear"])


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
