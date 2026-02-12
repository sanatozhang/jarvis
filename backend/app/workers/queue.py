"""
arq-based task queue for analysis jobs.

Usage:
  # Start the worker (separate process):
  arq app.workers.queue.WorkerSettings

  # Or use the fallback in-process mode (for dev):
  The tasks API uses BackgroundTasks as fallback when Redis is unavailable.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from app.workers.analysis_worker import run_analysis_pipeline
from app.db import database as db
from app.models.schemas import TaskStatus

logger = logging.getLogger("jarvis.queue")


async def analyze_task(ctx: Dict[str, Any], task_id: str, issue_id: str, agent_override: Optional[str] = None):
    """arq job: run the analysis pipeline for a single issue."""
    logger.info("Worker picked up task %s for issue %s", task_id, issue_id)

    async def on_progress(pct: int, msg: str):
        status = "analyzing"
        if pct <= 20:
            status = "downloading"
        elif pct <= 35:
            status = "decrypting"
        elif pct <= 55:
            status = "extracting"
        await db.update_task(task_id, status=status, progress=pct, message=msg)

    try:
        result = await run_analysis_pipeline(
            issue_id=issue_id,
            task_id=task_id,
            agent_override=agent_override,
            on_progress=on_progress,
        )
        await db.save_analysis(result.model_dump())
        await db.update_task(task_id, status="done", progress=100, message="分析完成")
        logger.info("Task %s completed successfully", task_id)
        return {"status": "done", "task_id": task_id}

    except Exception as e:
        logger.error("Task %s failed: %s", task_id, e, exc_info=True)
        await db.update_task(task_id, status="failed", error=str(e))
        return {"status": "failed", "task_id": task_id, "error": str(e)}


async def startup(ctx: Dict[str, Any]):
    """arq worker startup hook."""
    logger.info("arq worker starting...")
    await db.init_db()


async def shutdown(ctx: Dict[str, Any]):
    """arq worker shutdown hook."""
    logger.info("arq worker shutting down...")
    await db.close_db()


class WorkerSettings:
    """arq worker settings. Start with: arq app.workers.queue.WorkerSettings"""
    functions = [analyze_task]
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 3
    job_timeout = 600
    redis_settings = None  # Will be set from config at import time

    @classmethod
    def configure(cls, redis_url: str):
        from arq.connections import RedisSettings
        # Parse redis://host:port/db
        from urllib.parse import urlparse
        parsed = urlparse(redis_url)
        cls.redis_settings = RedisSettings(
            host=parsed.hostname or "localhost",
            port=parsed.port or 6379,
            database=int(parsed.path.lstrip("/") or 0),
        )
