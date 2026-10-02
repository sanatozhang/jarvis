"""Store port on jarvis' SQLAlchemy database (tables `mh_*`)."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Callable, List, Optional

from sqlalchemy import desc, select

from app.modulehub import models as m
from app.modulehub.core import states
from app.modulehub.ports import ReleaseRecord


def _to_record(row: m.MhRelease) -> ReleaseRecord:
    return ReleaseRecord(
        id=row.id, module=row.module, platform=row.platform, branch=row.branch, major=bool(row.major), kind=row.kind,
        state=row.state, failed_from=row.failed_from or "", version=row.version or "", previous_version=row.previous_version or "",
        sha256=row.sha256 or "", coordinate=row.coordinate or "", git_sha=row.git_sha or "", build_ref=row.build_ref or "",
        build_url=row.build_url or "", bump_pr_url=row.bump_pr_url or "", backport_pr_url=row.backport_pr_url or "",
        api_changes=json.loads(row.api_changes_json or "[]"), error=row.error or "", requested_by=row.requested_by or "",
        resume_count=row.resume_count or 0, created_at=row.created_at, updated_at=row.updated_at,
    )


_FIELDS = ("module", "platform", "branch", "major", "kind", "state", "failed_from", "version", "previous_version", "sha256",
           "coordinate", "git_sha", "build_ref", "build_url", "bump_pr_url", "backport_pr_url", "error", "requested_by", "resume_count")


def _apply(row: m.MhRelease, rec: ReleaseRecord) -> None:
    for f in _FIELDS:
        setattr(row, f, getattr(rec, f))
    row.api_changes_json = json.dumps(rec.api_changes)
    row.updated_at = datetime.utcnow()


class SqlStore:
    def __init__(self, session_factory: Callable):
        self._sf = session_factory

    async def create(self, rec: ReleaseRecord) -> ReleaseRecord:
        async with self._sf() as s:
            row = m.MhRelease()
            _apply(row, rec)
            s.add(row)
            await s.flush()
            s.add(m.MhReleaseEvent(release_id=row.id, state=rec.state, message="created"))
            await s.commit()
            rec.id, rec.created_at, rec.updated_at = row.id, row.created_at, row.updated_at
        return rec

    async def get(self, release_id: int) -> Optional[ReleaseRecord]:
        async with self._sf() as s:
            row = await s.get(m.MhRelease, release_id)
            return _to_record(row) if row else None

    async def save(self, rec: ReleaseRecord, event: str = "") -> None:
        async with self._sf() as s:
            row = await s.get(m.MhRelease, rec.id)
            _apply(row, rec)
            s.add(m.MhReleaseEvent(release_id=rec.id, state=rec.state, message=event or rec.state))
            await s.commit()

    async def find_active(self, module: str, platform: str) -> Optional[ReleaseRecord]:
        async with self._sf() as s:
            q = select(m.MhRelease).where(m.MhRelease.module == module, m.MhRelease.platform == platform,
                                          m.MhRelease.kind == "release", m.MhRelease.state.in_(states.ACTIVE)).limit(1)
            row = (await s.execute(q)).scalars().first()
            return _to_record(row) if row else None

    async def list_in_flight(self) -> List[ReleaseRecord]:
        async with self._sf() as s:
            q = select(m.MhRelease).where(m.MhRelease.state.in_(states.ACTIVE)).order_by(m.MhRelease.id)
            return [_to_record(r) for r in (await s.execute(q)).scalars()]

    async def list_recent(self, limit: int = 50) -> List[ReleaseRecord]:
        async with self._sf() as s:
            q = select(m.MhRelease).order_by(desc(m.MhRelease.id)).limit(limit)
            return [_to_record(r) for r in (await s.execute(q)).scalars()]

    async def last_released_version(self, module: str, platform: str, branch: str) -> str:
        async with self._sf() as s:
            q = select(m.MhRelease.version).where(
                m.MhRelease.module == module, m.MhRelease.platform == platform, m.MhRelease.branch == branch,
                m.MhRelease.kind == "release", m.MhRelease.state == states.DONE).order_by(desc(m.MhRelease.id)).limit(1)
            return (await s.execute(q)).scalar() or ""

    async def log_mirror(self, module: str, platform: str, branch: str, action: str, outcome: str) -> None:
        async with self._sf() as s:
            s.add(m.MhMirrorLog(module=module, platform=platform, branch=branch, action=action, outcome=outcome[:250]))
            await s.commit()
