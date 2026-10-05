"""modulehub: pure-scheduling subsystem that releases independent native modules.

The ONLY mount point is `register(app)`. Everything jarvis-specific lives in `adapters/`; `core/` and
`service.py` know nothing about jarvis (see docs/modulehub/architecture.md).
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

logger = logging.getLogger("jarvis.modulehub")


class Hub:
    """Lazily wires ports to jarvis adapters. Tests inject ready-made services instead."""

    def __init__(self, settings=None, *, store=None, releases=None, mirror=None):
        from app.modulehub.config import get_modulehub_settings

        self.settings = settings or get_modulehub_settings()
        self._store, self._releases, self._mirror = store, releases, mirror
        self._tasks: List[asyncio.Task] = []

    def _port_settings(self):
        from app.modulehub.config import to_port_settings

        return to_port_settings(self.settings)

    @property
    def store(self):
        if self._store is None:
            from app.db import database as db
            from app.modulehub.adapters.store_sqlalchemy import SqlStore

            self._store = SqlStore(db.get_session)
        return self._store

    def _scm(self):
        from app.modulehub.adapters.github_scm import GitHubScm, resolve_token

        return GitHubScm(resolve_token(self.settings.github_token))

    def _notifier(self):
        from app.modulehub.adapters.notifier import DmNotifier

        return DmNotifier(list(self.settings.notify_emails))

    @property
    def releases(self):
        if self._releases is None:
            from app.config import get_settings
            from app.modulehub.adapters.jenkins_runner import JenkinsBuildRunner
            from app.modulehub.service import ReleaseService
            from app.services.jenkins_client import build_client_from_settings

            runner = JenkinsBuildRunner(build_client_from_settings(get_settings()), self.settings.jenkins_job)
            self._releases = ReleaseService(store=self.store, build=runner, scm=self._scm(),
                                            notifier=self._notifier(), settings=self._port_settings())
        return self._releases

    @property
    def mirror(self):
        if self._mirror is None:
            from app.modulehub.service import MirrorService

            self._mirror = MirrorService(store=self.store, scm=self._scm(), notifier=self._notifier(), settings=self._port_settings())
        return self._mirror

    async def start(self) -> None:
        """Start the background loops (only when `modulehub.enabled`)."""
        if not self.settings.enabled:
            logger.info("modulehub disabled (set MODULEHUB_ENABLED=true or modulehub.enabled)")
            return
        from app.modulehub.workers.loops import run_forever

        self._tasks = [
            asyncio.create_task(run_forever(lambda: self.releases.tick_all(), self.settings.poll_interval_seconds, name="modulehub-poller")),
            asyncio.create_task(run_forever(lambda: self.mirror.sync(), self.settings.mirror_interval_minutes * 60, name="modulehub-mirror")),
        ]
        logger.info("modulehub workers started")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks = []


def register(app, hub: Optional[Hub] = None) -> Hub:
    """Mount routes and expose the hub on `app.state.modulehub` (lifespan calls `start()` / `stop()` on it)."""
    from app.modulehub.api.router import router

    app.state.modulehub = hub or Hub()
    app.include_router(router, prefix="/api/modulehub", tags=["ModuleHub"])
    return app.state.modulehub
