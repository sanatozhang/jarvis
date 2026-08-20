"""pr_conflict_resync 单测（2026-08-20）。

策略：BEHIND → 调服务端 update-branch API；DIRTY / update-branch 报错 → 只通知
不动代码；全程不碰本地 git（没有 subprocess 调 git，只调 gh）。
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime
from types import SimpleNamespace

import pytest


async def _setup(tmp_path, monkeypatch, *, enabled=True):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'cr.db'}")
    monkeypatch.setenv("CRASHGUARD_DATADOG_API_KEY", "")
    monkeypatch.setenv("CRASHGUARD_WARMUP_ON_STARTUP", "false")
    monkeypatch.setenv("CRASHGUARD_CONFLICT_RESYNC_ENABLED", "true" if enabled else "false")
    from app.config import get_settings
    from app.crashguard.config import get_crashguard_settings
    get_settings.cache_clear()
    get_crashguard_settings.cache_clear()
    from app.db.database import init_db
    from app.crashguard import models  # noqa: F401
    await init_db()


async def _seed_pr(pr_url: str, pr_status: str = "open", reviewer_emails: str = "[]"):
    from app.db.database import get_session
    from app.crashguard.models import CrashPullRequest

    async with get_session() as session:
        session.add(CrashPullRequest(
            analysis_id=1,
            datadog_issue_id=f"issue-{pr_url}",
            pr_url=pr_url,
            pr_status=pr_status,
            reviewer_emails=reviewer_emails,
            created_at=datetime.utcnow(),
        ))
        await session.commit()


def _fake_gh_factory(view_responses, update_branch_ok=True):
    """view_responses: dict pr_number -> {"mergeStateStatus":..., "state":"OPEN"}"""
    def _fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "view"]:
            pr_number = int(cmd[3])
            data = view_responses.get(pr_number, {"mergeStateStatus": "CLEAN", "state": "OPEN"})
            return SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
        if cmd[:4] == ["gh", "api", "-X", "PUT"]:
            if update_branch_ok:
                return SimpleNamespace(returncode=0, stdout="{}", stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="merge conflict")
        raise AssertionError(f"unexpected command: {cmd}")
    return _fake_run


@pytest.mark.asyncio
async def test_disabled_by_default_is_noop(tmp_path, monkeypatch):
    await _setup(tmp_path, monkeypatch, enabled=False)
    from app.crashguard.services.pr_conflict_resync import run_conflict_resync_sweep
    res = await run_conflict_resync_sweep()
    assert res == {"ok": True, "skipped": "disabled"}


@pytest.mark.asyncio
async def test_behind_pr_gets_updated_via_github_api(tmp_path, monkeypatch):
    await _setup(tmp_path, monkeypatch)
    await _seed_pr("https://github.com/Plaud-AI/plaud-native-android/pull/101")

    monkeypatch.setattr(subprocess, "run", _fake_gh_factory({
        101: {"mergeStateStatus": "BEHIND", "state": "OPEN"},
    }))

    from app.crashguard.services.pr_conflict_resync import run_conflict_resync_sweep
    res = await run_conflict_resync_sweep()
    assert res["updated"] == 1
    assert res["conflicts"] == 0


@pytest.mark.asyncio
async def test_dirty_pr_is_not_touched_only_notified(tmp_path, monkeypatch):
    await _setup(tmp_path, monkeypatch)
    await _seed_pr(
        "https://github.com/Plaud-AI/plaud-native-android/pull/202",
        reviewer_emails='["yancy@plaud.ai"]',
    )

    monkeypatch.setattr(subprocess, "run", _fake_gh_factory({
        202: {"mergeStateStatus": "DIRTY", "state": "OPEN"},
    }))

    sent = {}

    async def _fake_send(**kwargs):
        sent.update(kwargs)
        return True

    import app.crashguard.services.pr_conflict_resync as mod
    monkeypatch.setattr(mod, "send_message", _fake_send)

    res = await mod.run_conflict_resync_sweep()
    assert res["updated"] == 0
    assert res["conflicts"] == 1
    assert "yancy@plaud.ai" in sent.get("text", "")
    assert "pull/202" in sent.get("text", "")


@pytest.mark.asyncio
async def test_update_branch_failure_is_treated_as_conflict_not_error(tmp_path, monkeypatch):
    await _setup(tmp_path, monkeypatch)
    await _seed_pr("https://github.com/Plaud-AI/plaud-native-android/pull/303")

    monkeypatch.setattr(subprocess, "run", _fake_gh_factory(
        {303: {"mergeStateStatus": "BEHIND", "state": "OPEN"}},
        update_branch_ok=False,
    ))

    import app.crashguard.services.pr_conflict_resync as mod
    monkeypatch.setattr(mod, "send_message", lambda **kw: True)

    res = await mod.run_conflict_resync_sweep()
    assert res["updated"] == 0
    assert res["conflicts"] == 1
    assert res["errors"] == 0


@pytest.mark.asyncio
async def test_closed_pr_is_skipped_not_touched(tmp_path, monkeypatch):
    await _setup(tmp_path, monkeypatch)
    await _seed_pr("https://github.com/Plaud-AI/plaud-native-android/pull/404")

    monkeypatch.setattr(subprocess, "run", _fake_gh_factory({
        404: {"mergeStateStatus": "DIRTY", "state": "MERGED"},
    }))

    from app.crashguard.services.pr_conflict_resync import run_conflict_resync_sweep
    res = await run_conflict_resync_sweep()
    assert res["updated"] == 0
    assert res["conflicts"] == 0
    assert res["skipped"] == 1


@pytest.mark.asyncio
async def test_never_shells_out_to_git(tmp_path, monkeypatch):
    """核心安全约束：这个模块完全不允许调 git（更别提 rebase/merge），只走 gh API。"""
    await _setup(tmp_path, monkeypatch)
    await _seed_pr("https://github.com/Plaud-AI/plaud-native-android/pull/505")

    calls = []

    def _tracking_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "git":
            raise AssertionError("must never shell out to git")
        return _fake_gh_factory({505: {"mergeStateStatus": "BEHIND", "state": "OPEN"}})(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", _tracking_run)

    from app.crashguard.services.pr_conflict_resync import run_conflict_resync_sweep
    await run_conflict_resync_sweep()
    assert all(c[0] == "gh" for c in calls)
