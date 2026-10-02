"""HTTP API (contract: module-kit docs/orchestrator/api.md). Mounted at /api/modulehub."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.modulehub.core.release_rules import InvalidRequest
from app.modulehub.ports import ReleaseRecord
from app.modulehub.service import ReleaseConflict, ReleaseNotFound

router = APIRouter()


class ReleaseRequest(BaseModel):
    module: str
    platform: str
    branch: str = "main"
    major: bool = False


def _hub(request: Request):
    return request.app.state.modulehub


def _actor(request: Request) -> str:
    user = getattr(request.state, "user", None) or {}
    who = user.get("email") or user.get("username")
    if who:
        return who
    if _hub(request).settings.allow_anonymous:
        return "anonymous"
    raise HTTPException(status_code=401, detail="login required")


def _dto(r: ReleaseRecord) -> Dict[str, Any]:
    return {
        "id": r.id, "module": r.module, "platform": r.platform, "branch": r.branch, "major": r.major, "kind": r.kind,
        "state": r.state, "failedFrom": r.failed_from, "version": r.version, "previousVersion": r.previous_version,
        "coordinate": r.coordinate, "sha256": r.sha256, "buildUrl": r.build_url, "bumpPrUrl": r.bump_pr_url,
        "backportPrUrl": r.backport_pr_url, "apiChanges": r.api_changes, "error": r.error, "requestedBy": r.requested_by,
        "resumeCount": r.resume_count, "createdAt": r.created_at.isoformat() if r.created_at else None,
        "updatedAt": r.updated_at.isoformat() if r.updated_at else None,
    }


async def _call(coro):
    try:
        return await coro
    except InvalidRequest as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ReleaseConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ReleaseNotFound:
        raise HTTPException(status_code=404, detail="release not found")


@router.get("/modules")
async def list_modules(request: Request) -> List[Dict[str, str]]:
    return await _hub(request).releases.list_modules()


@router.get("/releases")
async def list_releases(request: Request, limit: int = 50) -> List[Dict[str, Any]]:
    return [_dto(r) for r in await _hub(request).store.list_recent(min(max(limit, 1), 200))]


@router.post("/releases:preview")
async def preview_release(body: ReleaseRequest, request: Request) -> Dict[str, Any]:
    actor = _actor(request)
    rec = await _call(_hub(request).releases.start(module=body.module, platform=body.platform, branch=body.branch,
                                                   major=body.major, actor=actor, dry_run=True))
    return _dto(rec)


@router.post("/releases", status_code=201)
async def start_release(body: ReleaseRequest, request: Request) -> Dict[str, Any]:
    actor = _actor(request)
    rec = await _call(_hub(request).releases.start(module=body.module, platform=body.platform, branch=body.branch,
                                                   major=body.major, actor=actor))
    return _dto(rec)


@router.get("/releases/{release_id:int}")
async def get_release(release_id: int, request: Request) -> Dict[str, Any]:
    rec: Optional[ReleaseRecord] = await _hub(request).store.get(release_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="release not found")
    return _dto(rec)


@router.post("/releases/{release_id:int}:resume")
async def resume_release(release_id: int, request: Request) -> Dict[str, Any]:
    actor = _actor(request)
    return _dto(await _call(_hub(request).releases.resume(release_id, actor)))


@router.post("/mirror:sync")
async def sync_mirror(request: Request) -> Dict[str, List[str]]:
    _actor(request)
    return {"results": await _hub(request).mirror.sync()}
