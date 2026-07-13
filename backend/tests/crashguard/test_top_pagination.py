"""/top 分页 + 后端过滤 + aggregates 单测。

底层逻辑：首页分页化后所有 filter/sort/search 都 push 到后端，aggregates
头部由后端给。本测试覆盖：
  - skip_dedup 在分页路径下生效（早晚报推过的 issue 也能列出来）
  - page 切片 / total_pages
  - aggregates 数值（p0/surge/new/fatal/non_fatal/totals）
  - search 子串匹配
  - sort_by 顺序
  - 旧调用方（未传 page）兼容：保留 dedup + 截断行为
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest


async def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'top.db'}")
    from app.config import get_settings
    from app.crashguard.config import get_crashguard_settings
    get_settings.cache_clear()
    get_crashguard_settings.cache_clear()
    from app.db.database import init_db
    from app.crashguard import models  # noqa: F401
    await init_db()


async def _seed_issues(today: date, count: int, fatality: str = "fatal", base_events: int = 100):
    """种入 count 个 issue + snapshot；events_count 用 base_events + i 区分排序。"""
    from app.db.database import get_session
    from app.crashguard.models import CrashIssue, CrashSnapshot
    async with get_session() as session:
        for i in range(count):
            iid = f"ddi_{fatality}_{i:03d}"
            session.add(CrashIssue(
                datadog_issue_id=iid,
                platform=("android" if i % 2 == 0 else "ios"),
                title=f"Crash#{i} {fatality}",
                kind="crash",
                fatality=fatality,
                status="open",
                first_seen_at=datetime.utcnow() - timedelta(days=30 + i),
            ))
            session.add(CrashSnapshot(
                datadog_issue_id=iid,
                snapshot_date=today,
                events_count=base_events + i,
                users_affected=10 + i,
                sessions_affected=5 + i,
                crash_free_impact_score=float(base_events + i) / 100.0,
                is_new_in_version=(i < 3),     # 前 3 个标 new → P0
                is_regression=False,
                is_surge=(i % 5 == 0),         # 每 5 个 1 个 surge
            ))
        await session.commit()


@pytest.mark.asyncio
async def test_pagination_slicing_and_total_pages(tmp_path, monkeypatch):
    """45 个 issue，page_size=40 → page1 40 个，page2 5 个，total_pages=2。"""
    await _setup(tmp_path, monkeypatch)
    today = date.today()
    await _seed_issues(today, 45)

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        r1 = client.get("/api/crash/top?page=1&page_size=40&sort_by=events")
        assert r1.status_code == 200
        j1 = r1.json()
        assert j1["total"] == 45
        assert j1["total_pages"] == 2
        assert len(j1["issues"]) == 40
        # 默认 events desc：最大 events_count 在前
        assert j1["issues"][0]["events_count"] >= j1["issues"][-1]["events_count"]

        r2 = client.get("/api/crash/top?page=2&page_size=40&sort_by=events")
        j2 = r2.json()
        assert j2["page"] == 2
        assert len(j2["issues"]) == 5


@pytest.mark.asyncio
async def test_skip_dedup_includes_recently_reported(tmp_path, monkeypatch):
    """已被早晚报推送过的 issue 在 /top 分页路径下也要出现（首页全集语义）。"""
    await _setup(tmp_path, monkeypatch)
    today = date.today()
    await _seed_issues(today, 5)

    # 模拟早报已推过 ddi_fatal_001（非 surge：测试 seed 里 i%5==0 才是 surge）
    # —— ranker 默认对非 surge 已推过的做 skip；分页路径下 skip_dedup=True 不 skip
    import json as _json
    from app.db.database import get_session
    from app.crashguard.models import CrashDailyReport
    async with get_session() as session:
        session.add(CrashDailyReport(
            report_date=today,
            report_type="morning",
            top_n=5,
            new_count=1,
            surge_count=0,
            regression_count=0,
            report_payload=_json.dumps({
                "issues": [{"datadog_issue_id": "ddi_fatal_001"}],
            }),
        ))
        await session.commit()

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        # 分页路径 → skip_dedup=True → 全部 5 个都返回
        r = client.get("/api/crash/top?page=1&page_size=40")
        j = r.json()
        ids = {x["datadog_issue_id"] for x in j["issues"]}
        assert "ddi_fatal_001" in ids
        assert j["total"] == 5

        # 旧路径（未传 page）→ skip_dedup=False → 非 surge 的 reported 被剔除
        r2 = client.get("/api/crash/top?limit=40")
        j2 = r2.json()
        ids2 = {x["datadog_issue_id"] for x in j2["issues"]}
        assert "ddi_fatal_001" not in ids2


@pytest.mark.asyncio
async def test_aggregates_counts(tmp_path, monkeypatch):
    """aggregates 计数：p0=3（is_new 标了前 3 个），fatal=10，non_fatal=5。"""
    await _setup(tmp_path, monkeypatch)
    today = date.today()
    await _seed_issues(today, 10, fatality="fatal")
    await _seed_issues(today, 5, fatality="non_fatal", base_events=50)

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        r = client.get("/api/crash/top?page=1&page_size=40")
        j = r.json()
        agg = j["aggregates"]
        assert agg["p0_count"] == 6   # 前 3 fatal + 前 3 non_fatal 都标 new
        assert agg["fatal_count"] == 10
        assert agg["non_fatal_count"] == 5
        assert agg["total_events"] > 0


@pytest.mark.asyncio
async def test_search_substring(tmp_path, monkeypatch):
    """search='Crash#3' 应只匹配 ddi_fatal_003。"""
    await _setup(tmp_path, monkeypatch)
    today = date.today()
    await _seed_issues(today, 10)

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        r = client.get("/api/crash/top?page=1&page_size=40&search=Crash%233 ")
        j = r.json()
        assert j["total"] == 1
        assert j["issues"][0]["datadog_issue_id"] == "ddi_fatal_003"


@pytest.mark.asyncio
async def test_platform_filter(tmp_path, monkeypatch):
    """platform=android 只返回 android（一半数据）。"""
    await _setup(tmp_path, monkeypatch)
    today = date.today()
    await _seed_issues(today, 10)

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        r = client.get("/api/crash/top?page=1&page_size=40&platform=android")
        j = r.json()
        # 10 个中偶数 idx (0,2,4,6,8) = 5 个 android
        assert j["total"] == 5
        for it in j["issues"]:
            assert it["platform"] == "android"


@pytest.mark.asyncio
async def test_generation_filter(tmp_path, monkeypatch):
    """generation=native 只留 native/未知代际；flutter service 的被过滤掉。

    未知代际（无 service 也无可解析 version）保守放行——不因分类失败漏报。
    """
    await _setup(tmp_path, monkeypatch)
    today = date.today()
    from app.db.database import get_session
    from app.crashguard.models import CrashIssue, CrashSnapshot

    async with get_session() as session:
        rows = [
            ("ddi_native", "plaud_android", ""),
            ("ddi_flutter", "plaud-flutter", ""),
            ("ddi_unknown", "", ""),
        ]
        for iid, service, version in rows:
            session.add(CrashIssue(
                datadog_issue_id=iid,
                platform="android",
                service=service,
                last_seen_version=version,
                title=f"Crash {iid}",
                kind="crash",
                fatality="fatal",
                status="open",
                first_seen_at=datetime.utcnow() - timedelta(days=1),
            ))
            session.add(CrashSnapshot(
                datadog_issue_id=iid,
                snapshot_date=today,
                events_count=10,
                users_affected=1,
                sessions_affected=1,
                crash_free_impact_score=0.1,
                is_new_in_version=False,
                is_regression=False,
                is_surge=False,
            ))
        await session.commit()

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        r = client.get("/api/crash/top?page=1&page_size=40&generation=native")
        j = r.json()
        ids = {x["datadog_issue_id"] for x in j["issues"]}
        assert ids == {"ddi_native", "ddi_unknown"}

        r2 = client.get("/api/crash/top?page=1&page_size=40&generation=flutter")
        j2 = r2.json()
        ids2 = {x["datadog_issue_id"] for x in j2["issues"]}
        assert ids2 == {"ddi_flutter", "ddi_unknown"}

        r3 = client.get("/api/crash/top?page=1&page_size=40")
        j3 = r3.json()
        assert {x["datadog_issue_id"] for x in j3["issues"]} == {"ddi_native", "ddi_flutter", "ddi_unknown"}


@pytest.mark.asyncio
async def test_backward_compat_legacy_call(tmp_path, monkeypatch):
    """未传 page → 走旧路径：仅 limit 截断 + dedup，不含 page/total_pages。"""
    await _setup(tmp_path, monkeypatch)
    today = date.today()
    await _seed_issues(today, 5)

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        r = client.get("/api/crash/top?limit=3")
        j = r.json()
        assert len(j["issues"]) == 3
        # 旧路径仍可访问 aggregates / total（新增字段不破坏老调用方）
        assert "aggregates" in j
        # page/total_pages 只在分页路径下返回
        assert "total_pages" not in j
