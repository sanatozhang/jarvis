"""
Background poll loop for Jenkins build status.

Every `poll_interval_seconds`:
  1. Find ReleaseBuild rows still in (queued, running).
  2. For each:
       - If still queued, ask /queue/item/<id>/api/json. If executable.number
         shows up, transition to running and record build_number + build_url.
       - If running, ask <build_url>/api/json. If `building == false`, settle
         to success/failure/aborted and capture artifact URLs.
  3. If `triggered_at` is older than `build_timeout_seconds`, force-error.

A single iteration is best-effort: per-row errors are logged and skipped
so one bad row never poisons the loop.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import List
from urllib.parse import urlsplit

from sqlalchemy import select

from app.config import get_settings
from app.db import database as db
from app.db.database import ReleaseBuild
from app.services.jenkins_client import JenkinsError, build_client_from_settings

logger = logging.getLogger("jarvis.release_poller")


def _rewrite_jenkins_url(jenkins_server: str, url: str) -> str:
    """Replace whatever host Jenkins reports (often `http://localhost:8080`,
    Jenkins's own configured base URL) with the actual server we hit.

    Returns the URL unchanged if it already points to our server.
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except Exception:
        return url
    if not parts.scheme:
        # Relative path → join with server.
        return jenkins_server.rstrip("/") + "/" + url.lstrip("/")
    # Always force our known server host onto the URL; preserve path/query.
    server = jenkins_server.rstrip("/")
    tail = parts.path or "/"
    if parts.query:
        tail = tail + "?" + parts.query
    return server + tail


async def _poll_one(jk, build: ReleaseBuild) -> None:
    """Update a single build row in-place. Caller commits the session."""
    server = build.jenkins_server
    if not server:
        return

    # --- still queued: poll the queue item until executable shows up ---
    if build.status == "queued":
        if build.jenkins_queue_id is None:
            return
        try:
            item = await jk.fetch_queue_item(server, build.jenkins_queue_id)
        except JenkinsError as e:
            logger.warning("queue item fetch failed (build=%s): %s", build.id, e)
            return
        if item.get("_gone"):
            # Queue item evicted; if we never captured build_number we can't
            # recover. Mark as error so user can re-trigger.
            build.status = "error"
            build.error_message = "Jenkins queue item disappeared before build_number was captured"
            build.finished_at = datetime.utcnow()
            return
        executable = item.get("executable")
        if executable and executable.get("number"):
            build.jenkins_build_number = int(executable["number"])
            raw_url = executable.get("url") or ""
            build.jenkins_build_url = _rewrite_jenkins_url(server, raw_url)
            build.status = "running"
            build.started_at = datetime.utcnow()
        return

    # --- running: poll build status ---
    if build.status == "running":
        if not build.jenkins_build_url:
            return
        # Idempotent rewrite (heals legacy rows where Jenkins returned
        # `http://localhost:8080/...` from its own internal base URL).
        fixed_url = _rewrite_jenkins_url(server, build.jenkins_build_url)
        if fixed_url != build.jenkins_build_url:
            build.jenkins_build_url = fixed_url
        try:
            data = await jk.fetch_build_status(build.jenkins_server, build.jenkins_build_url)
        except JenkinsError as e:
            logger.warning("build status fetch failed (build=%s): %s", build.id, e)
            return
        if data.get("building"):
            return
        result = (data.get("result") or "").upper()
        if result == "SUCCESS":
            build.status = "success"
            artifacts = data.get("artifacts") or []
            and_raw = jk.pick_artifact_url(build.jenkins_build_url, artifacts, "android") or ""
            ios_raw = jk.pick_artifact_url(build.jenkins_build_url, artifacts, "ios") or ""
            build.artifact_android_url = _rewrite_jenkins_url(server, and_raw)
            build.artifact_ios_url = _rewrite_jenkins_url(server, ios_raw)
        elif result in ("FAILURE", "UNSTABLE"):
            build.status = "failure"
            build.error_message = f"Jenkins result: {result}"
        elif result == "ABORTED":
            build.status = "aborted"
            build.error_message = "Jenkins build aborted"
        else:
            # NOT_BUILT / null with building=false → treat as error
            build.status = "error"
            build.error_message = f"Unexpected Jenkins result: {result!r}"
        build.finished_at = datetime.utcnow()


async def poll_once() -> int:
    """Run one polling tick. Returns number of rows examined."""
    settings = get_settings()
    if not settings.jenkins.enabled or not settings.jenkins.servers:
        return 0
    jk = build_client_from_settings(settings)
    timeout = settings.jenkins.build_timeout_seconds
    cutoff = datetime.utcnow() - timedelta(seconds=timeout)

    async with db.get_session() as s:
        rows: List[ReleaseBuild] = (await s.execute(
            select(ReleaseBuild).where(
                ReleaseBuild.status.in_(["queued", "running"])
            )
        )).scalars().all()
        examined = 0
        for row in rows:
            examined += 1
            # Hard timeout sweep.
            if row.triggered_at and row.triggered_at < cutoff and row.status in ("queued", "running"):
                row.status = "error"
                row.error_message = f"build timed out after {timeout}s"
                row.finished_at = datetime.utcnow()
                continue
            try:
                await _poll_one(jk, row)
            except Exception as e:                       # pragma: no cover — defensive
                logger.exception("poller per-row error (build=%s): %s", row.id, e)
        await s.commit()
    return examined


async def release_poller_loop() -> None:
    """Periodic background task. Started from `app.main` lifespan."""
    settings = get_settings()
    interval = max(5, int(settings.jenkins.poll_interval_seconds or 30))
    logger.info("release_poller started (interval=%ds, jenkins_enabled=%s)",
                interval, settings.jenkins.enabled)
    while True:
        try:
            await asyncio.sleep(interval)
            n = await poll_once()
            if n:
                logger.debug("release_poller tick: examined %d active build(s)", n)
        except asyncio.CancelledError:
            logger.info("release_poller cancelled")
            return
        except Exception as e:
            logger.warning("release_poller tick failed: %s", e)
            await asyncio.sleep(min(interval, 30))
