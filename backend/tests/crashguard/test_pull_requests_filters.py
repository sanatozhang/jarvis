"""/api/crash/pull-requests 的 repo(平台) 过滤修复 + generation 过滤单测。

背景（2026-07-13）：`CrashPullRequest.repo` 列存的是 repo_router 解析出的
sub-repo 逻辑名（如 "plaud-native-android"），跟前端传的裸平台名
"android"/"ios" 不是同一空间——旧实现直接做字符串精确匹配，选 Android/iOS
基本命不中任何行。修复后 `repo` 过滤改成反查 CrashIssue.platform；同时新增
`generation` 参数（复用 classify_generation）。
"""
from __future__ import annotations

from datetime import datetime

import pytest


async def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'prs.db'}")
    from app.config import get_settings
    from app.crashguard.config import get_crashguard_settings
    get_settings.cache_clear()
    get_crashguard_settings.cache_clear()
    from app.db.database import init_db
    from app.crashguard import models  # noqa: F401
    await init_db()


async def _seed(issue_id: str, platform: str, service: str, version: str, repo_col: str, pr_status: str = "draft"):
    from app.db.database import get_session
    from app.crashguard.models import CrashIssue, CrashPullRequest

    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id=issue_id,
            platform=platform,
            service=service,
            last_seen_version=version,
            title=f"Crash {issue_id}",
            kind="crash",
            fatality="fatal",
            status="open",
        ))
        session.add(CrashPullRequest(
            analysis_id=1,
            datadog_issue_id=issue_id,
            repo=repo_col,
            pr_status=pr_status,
            created_at=datetime.utcnow(),
        ))
        await session.commit()


@pytest.mark.asyncio
async def test_repo_filter_matches_by_issue_platform_not_repo_column(tmp_path, monkeypatch):
    """repo=android 应该靠 CrashIssue.platform 命中，即便 repo 列存的是 sub-repo 逻辑名。"""
    await _setup(tmp_path, monkeypatch)
    # repo 列故意写成跟前端传参不一样的 repo_router 逻辑名，模拟生产实况
    await _seed("ddi_native_android", "android", "plaud_android", "", "plaud-native-android")
    await _seed("ddi_legacy_android", "android", "", "3.16.0", "plaud-android")
    await _seed("ddi_ios", "ios", "plaud_ios", "", "plaud-native-ios")

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        r = client.get("/api/crash/pull-requests?repo=android")
        j = r.json()
        ids = {x["datadog_issue_id"] for x in j["items"]}
        assert ids == {"ddi_native_android", "ddi_legacy_android"}

        r2 = client.get("/api/crash/pull-requests?repo=ios")
        j2 = r2.json()
        assert {x["datadog_issue_id"] for x in j2["items"]} == {"ddi_ios"}


@pytest.mark.asyncio
async def test_generation_filter_conservative_unknown(tmp_path, monkeypatch):
    """generation=native 保留 native + 未知代际，剔除 flutter。"""
    await _setup(tmp_path, monkeypatch)
    await _seed("ddi_native", "android", "plaud_android", "", "plaud-native-android")
    await _seed("ddi_flutter", "android", "plaud-flutter", "", "plaud-android")
    await _seed("ddi_unknown", "android", "", "", "android")

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        r = client.get("/api/crash/pull-requests?generation=native")
        j = r.json()
        assert {x["datadog_issue_id"] for x in j["items"]} == {"ddi_native", "ddi_unknown"}

        r2 = client.get("/api/crash/pull-requests?generation=flutter")
        j2 = r2.json()
        assert {x["datadog_issue_id"] for x in j2["items"]} == {"ddi_flutter", "ddi_unknown"}


@pytest.mark.asyncio
async def test_app_repo_value_rejected(tmp_path, monkeypatch):
    """vestigial "app" 取值已从枚举里去掉，传了应该 422。"""
    await _setup(tmp_path, monkeypatch)

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        r = client.get("/api/crash/pull-requests?repo=app")
        assert r.status_code == 422
