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
        id=row.id, module=row.module, platforms=[p for p in (row.platforms or "").split(",") if p], branch=row.branch,
        major=bool(row.major), kind=row.kind, state=row.state, failed_from=row.failed_from or "", version=row.version or "",
        git_sha=row.git_sha or "", artifacts=json.loads(row.artifacts_json or "{}"),
        previous_versions=json.loads(row.previous_versions_json or "{}"), bump_prs=json.loads(row.bump_prs_json or "{}"),
        build_ref=row.build_ref or "", build_url=row.build_url or "", backport_pr_url=row.backport_pr_url or "",
        error=row.error or "", requested_by=row.requested_by or "", resume_count=row.resume_count or 0,
        created_at=row.created_at, updated_at=row.updated_at,
    )


_FIELDS = ("module", "branch", "major", "kind", "state", "failed_from", "version", "git_sha", "build_ref", "build_url",
           "backport_pr_url", "error", "requested_by", "resume_count")


def _apply(row: m.MhRelease, rec: ReleaseRecord) -> None:
    for f in _FIELDS:
        setattr(row, f, getattr(rec, f))
    row.platforms = ",".join(rec.platforms)
    row.artifacts_json = json.dumps(rec.artifacts, sort_keys=True)
    row.previous_versions_json = json.dumps(rec.previous_versions, sort_keys=True)
    row.bump_prs_json = json.dumps(rec.bump_prs, sort_keys=True)
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

    async def find_active(self, module: str) -> Optional[ReleaseRecord]:
        async with self._sf() as s:
            q = select(m.MhRelease).where(m.MhRelease.module == module, m.MhRelease.kind == "release",
                                          m.MhRelease.state.in_(states.ACTIVE)).limit(1)
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

    async def log_mirror(self, module: str, branch: str, action: str, outcome: str) -> None:
        async with self._sf() as s:
            s.add(m.MhMirrorLog(module=module, branch=branch, action=action, outcome=outcome[:250]))
            await s.commit()
