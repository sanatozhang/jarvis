"""
Release automation API.

Jarvis is a thin orchestrator here — it does NOT touch source code.
Two independent operations:

  - POST /api/release/branches
      ① mt fetch + checkout main + pull   (sync workspace to upstream main)
      ② mt checkout -b <branch>           (create release branch in every sub-repo)
      ③ mt push -u origin <branch>        (publish to remote)
      ④ Feishu notification → creator + admin notify_emails
      ⑤ mt checkout main                  (restore workspace state)

  - POST /api/release/builds
      Plain HTTP trigger against the least-busy Jenkins server. Jenkins's
      pipeline does the version bump, build, sign, upload internally.

Artifact downloads return a 302 redirect to the Jenkins URL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select, desc

from app.config import get_settings
from app.db import database as db
from app.db.database import Release, ReleaseBuild
from app.services.jenkins_client import JenkinsError, build_client_from_settings
from app.services.mt_runner import MtRunner, MtRunnerError, run_in_lock

logger = logging.getLogger("jarvis.api.release")
router = APIRouter()

BRANCH_RE = re.compile(r"^release/(\d+)\.(\d+)\.(\d+)_(\d{4})$")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class CreateBranchRequest(BaseModel):
    branch: str


class TriggerBuildRequest(BaseModel):
    branch: str
    target: str                                # "cn" / "global"
    # Per-target options (defaults per product spec, NOT per Jenkins UI defaults)
    is_online_package: bool = True             # IS_ONLINE_PACKAGE — both targets
    upload_to_github_release: bool = True      # UPLOAD_TO_GITHUB_RELEASE — both targets
    skip_asc_upload: bool = False              # SKIP_ASC_UPLOAD — global only
    android_multi_channel_pack: bool = True    # CN only; default true per product spec
    description: str = ""                      # optional free-text description


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _require_jenkins_configured():
    s = get_settings()
    if not s.jenkins.enabled:
        raise HTTPException(503, "Release module disabled (jenkins.enabled=false)")
    if not s.jenkins.servers:
        raise HTTPException(503, "No Jenkins servers configured")
    missing = [
        srv.url for srv in s.jenkins.servers
        if not srv.user or not srv.api_token
    ]
    if missing:
        raise HTTPException(
            503,
            f"Jenkins credentials missing for: {', '.join(missing)} "
            f"(check token_env vars in .env)",
        )


def _require_workspace() -> Path:
    s = get_settings()
    if not s.code_repo_app:
        raise HTTPException(503, "code_repo_app not configured")
    p = Path(s.code_repo_app)
    if not p.exists():
        raise HTTPException(503, f"code_repo_app path not found: {p}")
    return p


def _user_email(request: Request) -> str:
    """Resolve who triggered the action.

    Priority:
      1. `request.state.user` populated by SSO AuthMiddleware (production)
      2. First entry in `ADMIN_EMAILS` env (local-dev fallback when SSO is off
         — single admin running locally, attribution is unambiguous)
      3. literal "anonymous"
    """
    user = getattr(request.state, "user", None) or {}
    email = user.get("email") or user.get("username")
    if email:
        return email
    s = get_settings()
    if s.sso.admin_emails:
        return s.sso.admin_emails[0]
    return "anonymous"


def _release_to_dict(r: Release) -> Dict[str, Any]:
    return {
        "id": r.id,
        "branch": r.branch,
        "version": r.version,
        "date_tag": r.date_tag,
        "repos": json.loads(r.repos_json or "[]"),
        "created_by": r.created_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "status": r.status,
    }


def _build_to_dict(b: ReleaseBuild) -> Dict[str, Any]:
    return {
        "id": b.id,
        "branch": b.branch,
        "target": b.target,
        "android_multi_channel": b.android_multi_channel,
        "params": json.loads(b.params_json or "{}"),
        "jenkins_server": b.jenkins_server,
        "jenkins_job": b.jenkins_job,
        "jenkins_queue_id": b.jenkins_queue_id,
        "jenkins_build_number": b.jenkins_build_number,
        "jenkins_build_url": b.jenkins_build_url,
        "status": b.status,
        "started_at": b.started_at.isoformat() if b.started_at else None,
        "finished_at": b.finished_at.isoformat() if b.finished_at else None,
        "error_message": b.error_message,
        "artifact_android_url": b.artifact_android_url,
        "artifact_ios_url": b.artifact_ios_url,
        "triggered_by": b.triggered_by,
        "triggered_at": b.triggered_at.isoformat() if b.triggered_at else None,
    }


def _parse_branch(branch: str) -> Optional[Dict[str, str]]:
    m = BRANCH_RE.match(branch)
    if not m:
        return None
    return {
        "version": f"{m.group(1)}.{m.group(2)}.{m.group(3)}",
        "date_tag": m.group(4),
    }


async def _notify_feishu_branch_created(branch: str, creator: str, commits: Dict[str, str]) -> None:
    """Best-effort 飞书 notification. Failure is logged but never blocks the API."""
    settings = get_settings()
    recipients: List[str] = []
    if creator and "@" in creator:
        recipients.append(creator)
    for e in settings.jenkins.notify_emails or []:
        if e and e != creator and e not in recipients:
            recipients.append(e)
    if not recipients:
        return

    commit_lines = "\n".join(f"  {name}: {sha[:8]}" for name, sha in commits.items())
    text = (
        f"[Release] 新分支已创建：{branch}\n"
        f"创建人：{creator}\n"
        f"子仓 HEAD：\n{commit_lines}"
    )
    try:
        from app.services.feishu_cli import send_message
        for email in recipients:
            try:
                ok = await send_message(email=email, text=text)
                if not ok:
                    logger.warning("Feishu notify to %s returned False", email)
            except Exception as e:
                logger.warning("Feishu notify to %s failed: %s", email, e)
    except Exception as e:
        logger.warning("Feishu notify skipped (import/send error): %s", e)


# ---------------------------------------------------------------------------
# Branch endpoints
# ---------------------------------------------------------------------------
@router.post("/branches")
async def create_branch(req: CreateBranchRequest, request: Request):
    """Create a release branch in all sub-repos via mt, push, then restore workspace to main."""
    parsed = _parse_branch(req.branch)
    if not parsed:
        raise HTTPException(400, "Branch must match `release/X.Y.Z_MMDD` (e.g. release/3.2.0_1222)")

    workspace = _require_workspace()

    # Cheap pre-check (race-protected again under the lock).
    async with db.get_session() as s:
        existing = (await s.execute(
            select(Release).where(Release.branch == req.branch)
        )).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(409, f"Branch already created: {req.branch}")

    settings = get_settings()
    mt = MtRunner(workspace, mt_bin=settings.jenkins.mt_bin)

    def _do_create() -> Dict[str, str]:
        # 1) Sync to upstream main.
        mt.reset_workspace()
        mt.checkout_main_and_pull()
        # 2) Create + push the release branch.
        try:
            mt.checkout_new_branch(req.branch)
        except MtRunnerError:
            mt.delete_local_branch(req.branch)
            raise
        try:
            mt.push_branch(req.branch, set_upstream=True)
        except MtRunnerError:
            mt.delete_local_branch(req.branch)
            raise
        # 3) Capture HEAD of every sub-repo for the audit row.
        commits = mt.get_commits()
        # 4) Restore workspace to main so subsequent reads (crashguard / agent
        #    orchestrator / repo_updater) see the canonical state.
        try:
            mt.checkout_existing_branch("main")
        except MtRunnerError as e:
            # Don't fail the request — branch is already pushed. Just log loudly.
            logger.warning("post-create restore to main failed: %s", e)
        return commits

    try:
        commits = await run_in_lock(workspace, _do_create)
    except TimeoutError:
        raise HTTPException(503, "Workspace busy; please retry in a moment")
    except MtRunnerError as e:
        logger.error("create_branch failed: %s", e)
        raise HTTPException(500, f"mt failed: {e}")

    creator = _user_email(request)
    repos_payload = [{"name": k, "commit_sha": v} for k, v in commits.items()]
    record = Release(
        branch=req.branch,
        version=parsed["version"],
        date_tag=parsed["date_tag"],
        repos_json=json.dumps(repos_payload, ensure_ascii=False),
        created_by=creator,
        created_at=datetime.utcnow(),
        status="created",
    )
    async with db.get_session() as s:
        s.add(record)
        await s.commit()
        await s.refresh(record)

    # Fire-and-forget Feishu (non-blocking — we still return success even if 飞书 is down).
    asyncio.create_task(_notify_feishu_branch_created(req.branch, creator, commits))
    return _release_to_dict(record)


@router.get("/branches")
async def list_branches(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    async with db.get_session() as s:
        rows = (await s.execute(
            select(Release).order_by(desc(Release.created_at)).limit(limit).offset(offset)
        )).scalars().all()
        total_rows = (await s.execute(select(Release))).scalars().all()
    return {"items": [_release_to_dict(r) for r in rows], "total": len(total_rows)}


@router.get("/branches/{branch_path:path}")
async def get_branch(branch_path: str):
    async with db.get_session() as s:
        row = (await s.execute(
            select(Release).where(Release.branch == branch_path)
        )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"branch not found: {branch_path}")
    return _release_to_dict(row)


# ---------------------------------------------------------------------------
# Build endpoints
# ---------------------------------------------------------------------------
@router.post("/builds")
async def trigger_build(req: TriggerBuildRequest, request: Request):
    """Thin Jenkins trigger. Pipeline owns version bump + build + upload."""
    _require_jenkins_configured()
    if req.target not in ("cn", "global"):
        raise HTTPException(400, "target must be 'cn' or 'global'")
    # Format check only — we no longer require the branch to be Jarvis-registered.
    # Users may build pre-existing release branches that were created outside
    # Jarvis. Jenkins itself will fail-loudly if the branch isn't on remote.
    if _parse_branch(req.branch) is None:
        raise HTTPException(400, "Branch must match `release/X.Y.Z_MMDD`")
    settings = get_settings()

    # De-dup: only one (branch, target) build can be in-progress.
    async with db.get_session() as s:
        in_progress = (await s.execute(
            select(ReleaseBuild).where(
                ReleaseBuild.branch == req.branch,
                ReleaseBuild.target == req.target,
                ReleaseBuild.status.in_(["pending", "queued", "running"]),
            )
        )).scalars().first()
        if in_progress is not None:
            raise HTTPException(409, f"build already in progress: id={in_progress.id}")

    # 3) Pick least-busy Jenkins.
    jk = build_client_from_settings(settings)
    try:
        server = await jk.pick_least_busy_server()
    except JenkinsError as e:
        raise HTTPException(502, f"all Jenkins servers unreachable: {e}")

    job = settings.jenkins.job_cn if req.target == "cn" else settings.jenkins.job_global

    # Jenkins jobs expect FOUR separate branch parameters (one per sub-repo).
    # The release branch is the same on all sub-repos, so we fan the same
    # value into all four slots.
    def _b(v: bool) -> str:
        return "true" if v else "false"

    common_params: Dict[str, Any] = {
        "flutter_common": req.branch,
        "android_branch": req.branch,
        "ios_branch": req.branch,
        "description": req.description,
        "IS_ONLINE_PACKAGE": _b(req.is_online_package),
        "UPLOAD_TO_GITHUB_RELEASE": _b(req.upload_to_github_release),
    }
    if req.target == "global":
        params: Dict[str, Any] = {
            **common_params,
            "flutter_global": req.branch,
            "SKIP_ASC_UPLOAD": _b(req.skip_asc_upload),
        }
    else:  # cn
        params = {
            **common_params,
            "flutter_cn": req.branch,
            "android_multi_channel_pack": _b(req.android_multi_channel_pack),
        }

    try:
        queue_id, _ = await jk.trigger_build(server, job, params)
    except JenkinsError as e:
        logger.error("Jenkins trigger failed on %s: %s", server, e)
        raise HTTPException(502, f"Jenkins trigger failed: {e}")

    rec = ReleaseBuild(
        branch=req.branch,
        target=req.target,
        android_multi_channel=req.android_multi_channel_pack and req.target == "cn",
        params_json=json.dumps(params, ensure_ascii=False),
        jenkins_server=server,
        jenkins_job=job,
        jenkins_queue_id=queue_id,
        status="queued",
        triggered_by=_user_email(request),
        triggered_at=datetime.utcnow(),
    )
    async with db.get_session() as s:
        s.add(rec)
        await s.commit()
        await s.refresh(rec)
    return _build_to_dict(rec)


@router.get("/builds")
async def list_builds(
    branch: Optional[str] = None,
    target: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    async with db.get_session() as s:
        q = select(ReleaseBuild)
        if branch:
            q = q.where(ReleaseBuild.branch == branch)
        if target:
            q = q.where(ReleaseBuild.target == target)
        if status:
            q = q.where(ReleaseBuild.status == status)
        q = q.order_by(desc(ReleaseBuild.triggered_at)).limit(limit).offset(offset)
        rows = (await s.execute(q)).scalars().all()
    return {"items": [_build_to_dict(b) for b in rows]}


@router.get("/builds/{build_id}")
async def get_build(build_id: int):
    async with db.get_session() as s:
        row = (await s.execute(
            select(ReleaseBuild).where(ReleaseBuild.id == build_id)
        )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"build {build_id} not found")
    return _build_to_dict(row)


@router.get("/builds/{build_id}/artifacts/{platform}")
async def download_artifact(build_id: int, platform: str):
    """302 redirect to the Jenkins artifact URL (we don't proxy the binary)."""
    if platform not in ("android", "ios"):
        raise HTTPException(400, "platform must be 'android' or 'ios'")
    async with db.get_session() as s:
        row = (await s.execute(
            select(ReleaseBuild).where(ReleaseBuild.id == build_id)
        )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"build {build_id} not found")
    if row.status != "success":
        raise HTTPException(400, f"build {build_id} not finished (status={row.status})")
    url = row.artifact_android_url if platform == "android" else row.artifact_ios_url
    if not url:
        raise HTTPException(404, f"no {platform} artifact recorded for build {build_id}")
    return RedirectResponse(url=url, status_code=302)
