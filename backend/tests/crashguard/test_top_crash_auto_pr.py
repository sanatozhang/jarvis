"""Top crash 自动 PR 单测：路径覆盖。

不真调 pr_drafter，monkeypatch 替成 fake；只验证扫描逻辑、过滤链、节流。
"""
from __future__ import annotations

import pytest
from datetime import datetime

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession
from app.crashguard import models as _crashguard  # noqa
from app.crashguard.models import CrashAnalysis, CrashIssue, CrashPullRequest
from app.db import database as db_mod
from app.db.database import Base


@pytest.fixture
async def setup_db():
    """每个 test 全新内存 DB + 注入 get_session。"""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Sessionmaker = async_sessionmaker(eng, expire_on_commit=False, class_=AsyncSession)
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _patched():
        async with Sessionmaker() as s:
            yield s

    # 关键：service 模块在导入时 from app.db.database import get_session，
    # 所以同时 patch 该 service 模块里的引用，不只是 db_mod
    import app.crashguard.services.top_crash_auto_pr as svc_mod
    orig_db = db_mod.get_session
    orig_svc = getattr(svc_mod, "get_session", None)
    db_mod.get_session = _patched
    svc_mod.get_session = _patched
    try:
        yield Sessionmaker
    finally:
        db_mod.get_session = orig_db
        if orig_svc is not None:
            svc_mod.get_session = orig_svc
        await eng.dispose()


async def _seed_issue(session, ev: int, did: str, kind="crash"):
    iss = CrashIssue(
        datadog_issue_id=did, title=f"crash {did}", platform="flutter",
        status="open", kind=kind, total_events=ev, fatality="fatal",
        first_seen_at=datetime.utcnow(), last_seen_at=datetime.utcnow(),
    )
    session.add(iss)
    await session.flush()
    return iss


async def _seed_analysis(session, did: str, fea: float, status="success"):
    import uuid
    ana = CrashAnalysis(
        datadog_issue_id=did, analysis_run_id=str(uuid.uuid4()),
        status=status, feasibility_score=fea,
        agent_name="claude_code", fix_suggestion="x", followup_question="",
    )
    session.add(ana)
    await session.flush()
    return ana


async def _seed_pr(session, did: str, ana_id: int, pr_status="open"):
    pr = CrashPullRequest(
        analysis_id=ana_id, datadog_issue_id=did, repo="flutter",
        pr_url=f"https://github.com/x/y/pull/{ana_id}", pr_status=pr_status,
    )
    session.add(pr)
    await session.flush()
    return pr


async def _enable_and_get_fn():
    from app.crashguard import config as cfg_mod
    cfg_mod.get_crashguard_settings.cache_clear()
    s = cfg_mod.get_crashguard_settings()
    s.top_crash_auto_pr_enabled = True
    s.pr_enabled = True
    s.top_crash_auto_pr_threshold = 0.5
    s.top_crash_auto_pr_top_n = 20
    s.top_crash_auto_pr_max_per_tick = 3
    s.top_crash_auto_pr_retry_on_closed = False
    from app.crashguard.services.top_crash_auto_pr import run_top_crash_auto_pr_tick
    return run_top_crash_auto_pr_tick


@pytest.mark.asyncio
async def test_kill_switch_off(setup_db):
    from app.crashguard import config as cfg_mod
    cfg_mod.get_crashguard_settings.cache_clear()
    s = cfg_mod.get_crashguard_settings()
    s.top_crash_auto_pr_enabled = False
    from app.crashguard.services.top_crash_auto_pr import run_top_crash_auto_pr_tick
    res = await run_top_crash_auto_pr_tick()
    assert res["actioned"] == 0
    assert res.get("skipped_reason") == "kill_switch_off"


@pytest.mark.asyncio
async def test_pr_enabled_off(setup_db):
    from app.crashguard import config as cfg_mod
    cfg_mod.get_crashguard_settings.cache_clear()
    s = cfg_mod.get_crashguard_settings()
    s.top_crash_auto_pr_enabled = True
    s.pr_enabled = False
    from app.crashguard.services.top_crash_auto_pr import run_top_crash_auto_pr_tick
    res = await run_top_crash_auto_pr_tick()
    assert res["actioned"] == 0
    assert res.get("skipped_reason") == "pr_enabled_off"


@pytest.mark.asyncio
async def test_skips_issue_with_open_pr(setup_db, monkeypatch):
    fn = await _enable_and_get_fn()

    async def fake_draft(*args, **kwargs):
        raise AssertionError("should not be called for issue with active PR")

    monkeypatch.setattr(
        "app.crashguard.services.pr_drafter.draft_prs_multi", fake_draft,
    )

    Sm = setup_db
    async with Sm() as s:
        iss = await _seed_issue(s, 1000, "did-1")
        ana = await _seed_analysis(s, "did-1", 0.8)
        await _seed_pr(s, "did-1", ana.id, "open")
        await s.commit()

    res = await fn()
    assert res["actioned"] == 0
    assert any("has_active_pr" in x for x in res["skipped"])


@pytest.mark.asyncio
async def test_skips_low_feasibility(setup_db, monkeypatch):
    fn = await _enable_and_get_fn()
    Sm = setup_db
    async with Sm() as s:
        iss = await _seed_issue(s, 1000, "did-1")
        ana = await _seed_analysis(s, "did-1", 0.4)  # < 0.5
        await s.commit()

    res = await fn()
    assert res["actioned"] == 0
    assert any("fea_0.40_lt_0.50" in x for x in res["skipped"])


@pytest.mark.asyncio
async def test_skips_no_analysis(setup_db, monkeypatch):
    fn = await _enable_and_get_fn()
    Sm = setup_db
    async with Sm() as s:
        iss = await _seed_issue(s, 1000, "did-1")
        await s.commit()

    res = await fn()
    assert res["actioned"] == 0
    assert any("no_success_analysis" in x for x in res["skipped"])


@pytest.mark.asyncio
async def test_max_per_tick_caps(setup_db, monkeypatch):
    fn = await _enable_and_get_fn()

    call_count = {"n": 0}
    async def fake_draft(ana_id, approver):
        call_count["n"] += 1
        return {
            "ok": True,
            "prs": [{"ok": True, "pr_url": f"https://github.com/x/y/pull/{ana_id}"}],
            "succeeded": 1, "failed": 0, "total": 1,
        }
    monkeypatch.setattr(
        "app.crashguard.services.pr_drafter.draft_prs_multi", fake_draft,
    )

    Sm = setup_db
    async with Sm() as s:
        for i in range(6):
            iss = await _seed_issue(s, 1000 - i, f"did-{i}")
            await _seed_analysis(s, f"did-{i}", 0.7)
        await s.commit()

    res = await fn()
    # max_per_tick=3 → 应该只 actioned 3
    assert res["actioned"] == 3
    assert call_count["n"] == 3
    assert len(res["pr_urls"]) == 3


@pytest.mark.asyncio
async def test_skips_closed_pr_no_retry(setup_db, monkeypatch):
    fn = await _enable_and_get_fn()
    Sm = setup_db
    async with Sm() as s:
        iss = await _seed_issue(s, 1000, "did-1")
        ana = await _seed_analysis(s, "did-1", 0.8)
        await _seed_pr(s, "did-1", ana.id, "closed")
        await s.commit()

    res = await fn()
    assert res["actioned"] == 0
    assert any("has_closed_no_retry" in x for x in res["skipped"])


@pytest.mark.asyncio
async def test_retries_closed_pr_when_enabled(setup_db, monkeypatch):
    fn = await _enable_and_get_fn()
    from app.crashguard import config as cfg_mod
    cfg_mod.get_crashguard_settings().top_crash_auto_pr_retry_on_closed = True

    async def fake_draft(ana_id, approver):
        return {"ok": True, "prs": [{"ok": True, "pr_url": "u"}], "succeeded": 1, "failed": 0, "total": 1}
    monkeypatch.setattr(
        "app.crashguard.services.pr_drafter.draft_prs_multi", fake_draft,
    )

    Sm = setup_db
    async with Sm() as s:
        iss = await _seed_issue(s, 1000, "did-1")
        ana = await _seed_analysis(s, "did-1", 0.8)
        await _seed_pr(s, "did-1", ana.id, "closed")
        await s.commit()

    res = await fn()
    assert res["actioned"] == 1
