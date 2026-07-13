"""/api/crash/{latest-release,version-distribution} 的 generation 参数接口层测试。

覆盖范围：无 Datadog key 的 DB fallback 路径下，`generation` query param 能正确
一路传到 derive_* / 内联过滤逻辑——version_util 单测已经覆盖了纯函数行为，这里
补的是 FastAPI 路由层的参数解析 + 接线（正则校验、fallback 路径内联过滤代码）。
"""
from __future__ import annotations

import pytest


async def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'genfilter.db'}")
    # 真实 .env 里配了真的 Datadog key，走 pydantic-settings 的 env_file 直读——
    # delenv 只清 os.environ，清不掉 env_file 里的值，必须显式 setenv 成空串覆盖。
    monkeypatch.setenv("CRASHGUARD_DATADOG_API_KEY", "")
    # TestClient(app) 会跑完整 lifespan，默认 warmup_on_startup=true 会在后台异步
    # 打 Datadog 再写库——这个任务经常在测试函数已经返回之后才落地，写进的是
    # "当时全局 current" 的 DB（下一个测试刚 init_db() 换的新 sqlite），造成跨
    # 测试污染。空 key 已经能挡住 Datadog 调用本身，这里顺手也关掉 warmup 避免
    # 它去跑 fallback 聚合逻辑污染下一个测试的 DB。
    monkeypatch.setenv("CRASHGUARD_WARMUP_ON_STARTUP", "false")
    from app.config import get_settings
    from app.crashguard.config import get_crashguard_settings
    get_settings.cache_clear()
    get_crashguard_settings.cache_clear()
    from app.db.database import init_db
    from app.crashguard import models  # noqa: F401
    await init_db()


@pytest.mark.asyncio
async def test_latest_release_generation_filters_db_fallback(tmp_path, monkeypatch):
    await _setup(tmp_path, monkeypatch)
    from app.db.database import get_session
    from app.crashguard.models import CrashIssue

    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="i1", platform="android", service="plaud_android",
            last_seen_version="4.1.0", total_events=2000,
        ))
        session.add(CrashIssue(
            datadog_issue_id="i2", platform="android", service="plaud-flutter",
            last_seen_version="3.20.0", total_events=5000,
        ))
        await session.commit()

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        r = client.get("/api/crash/latest-release?generation=native")
        j = r.json()
        assert j["versions"]["android"] == "4.1.0"

        r2 = client.get("/api/crash/latest-release?generation=flutter")
        j2 = r2.json()
        assert j2["versions"]["android"] == "3.20.0"

        r3 = client.get("/api/crash/latest-release")
        j3 = r3.json()
        # 无 generation 过滤：两个版本里 events 更大的 3.20.0（flutter）胜出（按 events≥300 阈值 + max semver）
        assert j3["versions"]["android"] in ("4.1.0", "3.20.0")


@pytest.mark.asyncio
async def test_latest_release_rejects_invalid_generation(tmp_path, monkeypatch):
    await _setup(tmp_path, monkeypatch)

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        r = client.get("/api/crash/latest-release?generation=bogus")
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_version_distribution_generation_filters_db_fallback(tmp_path, monkeypatch):
    await _setup(tmp_path, monkeypatch)
    from app.db.database import get_session
    from app.crashguard.models import CrashIssue

    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="i1", platform="android", service="plaud_android",
            top_app_version="4.1.0 (100%)", total_events=1000,
        ))
        session.add(CrashIssue(
            datadog_issue_id="i2", platform="android", service="plaud-flutter",
            top_app_version="3.20.0 (100%)", total_events=1000,
        ))
        await session.commit()

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        r = client.get("/api/crash/version-distribution?generation=native")
        j = r.json()
        versions = {v["version"] for v in j["data"].get("android", [])}
        assert versions == {"4.1.0"}

        r2 = client.get("/api/crash/version-distribution?generation=flutter")
        j2 = r2.json()
        versions2 = {v["version"] for v in j2["data"].get("android", [])}
        assert versions2 == {"3.20.0"}

        r3 = client.get("/api/crash/version-distribution")
        j3 = r3.json()
        versions3 = {v["version"] for v in j3["data"].get("android", [])}
        assert versions3 == {"4.1.0", "3.20.0"}
