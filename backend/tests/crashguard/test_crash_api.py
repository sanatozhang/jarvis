"""Tests for crashguard.api.crash — issue detail generation field."""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker


@pytest.fixture
async def patched_session(db_engine):
    """Patch the app module's session factory to use the test engine."""
    import app.db.database as db_mod
    import app.crashguard.models  # noqa: F401

    async with db_engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.create_all)
    original = db_mod._session_factory
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    db_mod._session_factory = factory
    yield factory
    db_mod._session_factory = original


@pytest.mark.asyncio
async def test_issue_detail_includes_generation_field(patched_session):
    """Test that issue detail response includes generation field."""
    from app.crashguard.api.crash import get_issue_detail
    from app.crashguard.models import CrashIssue

    async with patched_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="native-2",
            platform="ANDROID",
            service="plaud_android",
            last_seen_version="4.0.100",
            title="native crash",
            stack_fingerprint="fpx",
        ))
        await session.commit()

    detail = await get_issue_detail("native-2")
    assert "generation" in detail
    assert detail["generation"] == "native"


@pytest.mark.asyncio
async def test_issue_detail_generation_flutter(patched_session):
    """Test that Flutter issue gets 'flutter' generation."""
    from app.crashguard.api.crash import get_issue_detail
    from app.crashguard.models import CrashIssue

    async with patched_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="flutter-1",
            platform="ANDROID",
            service="plaud-flutter",
            last_seen_version="3.16.0-634",
            title="flutter crash",
            stack_fingerprint="fpx",
        ))
        await session.commit()

    detail = await get_issue_detail("flutter-1")
    assert detail["generation"] == "flutter"


@pytest.mark.asyncio
async def test_issue_detail_generation_fallback_to_version(patched_session):
    """Test generation classification falls back to version when service is missing."""
    from app.crashguard.api.crash import get_issue_detail
    from app.crashguard.models import CrashIssue

    async with patched_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="test-3",
            platform="ANDROID",
            service="",
            last_seen_version="4.0.100",
            title="native crash via version",
            stack_fingerprint="fpx",
        ))
        await session.commit()

    detail = await get_issue_detail("test-3")
    assert detail["generation"] == "native"


@pytest.mark.asyncio
async def test_issue_detail_generation_unknown(patched_session):
    """Test generation is empty string when both service and version are missing."""
    from app.crashguard.api.crash import get_issue_detail
    from app.crashguard.models import CrashIssue

    async with patched_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="test-4",
            platform="ANDROID",
            service="",
            last_seen_version="",
            title="unknown generation crash",
            stack_fingerprint="fpx",
        ))
        await session.commit()

    detail = await get_issue_detail("test-4")
    assert detail["generation"] == ""


@pytest.mark.asyncio
async def test_issue_detail_includes_top_page_field(patched_session):
    """detail response 必须回传 top_page，前端才能展示卡顿页面分布。"""
    from app.crashguard.api.crash import get_issue_detail
    from app.crashguard.models import CrashIssue

    async with patched_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="jank-page-1",
            platform="ANDROID",
            kind="jank",
            title="Jank @ SomeClass",
            stack_fingerprint="fpx",
            top_page="fileDetail (60%), home (40%)",
        ))
        await session.commit()

    detail = await get_issue_detail("jank-page-1")
    assert detail["top_page"] == "fileDetail (60%), home (40%)"


@pytest.mark.asyncio
async def test_issue_detail_includes_fatality_and_kind_fields(patched_session):
    """detail response 必须回传 fatality/kind，前端才能在详情区标注"这是卡顿"。"""
    from app.crashguard.api.crash import get_issue_detail
    from app.crashguard.models import CrashIssue

    async with patched_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="jank-kind-1",
            platform="ios",
            kind="jank",
            fatality="jank",
            title="Jank @ SomeFunc",
            stack_fingerprint="fpk",
        ))
        await session.commit()

    detail = await get_issue_detail("jank-kind-1")
    assert detail["fatality"] == "jank"
    assert detail["kind"] == "jank"


@pytest.mark.asyncio
async def test_get_top_search_matches_top_page(patched_session):
    """search 参数必须能按 top_page 命中——工作流：在 Datadog 看板看到某页面卡顿多，
    把页面名粘到 Apollo 搜索框，定位到对应 issue。"""
    from datetime import date

    from app.crashguard.api.crash import get_top
    from app.crashguard.models import CrashIssue, CrashSnapshot

    today = date.today()
    async with patched_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="jank-page-2",
            platform="ANDROID",
            kind="jank",
            fatality="jank",
            title="Jank @ SomeOtherClass",
            stack_fingerprint="fpy",
            top_page="fileDetail (100%)",
        ))
        session.add(CrashSnapshot(
            datadog_issue_id="jank-page-2",
            snapshot_date=today,
            events_count=5,
        ))
        await session.commit()

    result = await get_top(
        target_date=today, kinds="all", page=1, page_size=40, search="filedetail", generation="",
    )
    ids = [it["datadog_issue_id"] for it in result["issues"]]
    assert "jank-page-2" in ids

    result_miss = await get_top(
        target_date=today, kinds="all", page=1, page_size=40, search="nomatch", generation="",
    )
    ids_miss = [it["datadog_issue_id"] for it in result_miss["issues"]]
    assert "jank-page-2" not in ids_miss
