"""_datadog_url_for() 卡顿(jank) 跳转链接修复单测（2026-07-21）。

背景：卡顿 issue 的 datadog_issue_id 是 jank_ingester.py 本地算出来的聚合键
（"jank:<sha1前16位>"），Datadog 侧根本没有这个 Error Tracking issue —— 点击
"Open in Datadog" 跳 /error-tracking/issue/jank:xxx 必定 404。卡顿改跳 Logs
Explorer（真实存在的数据源），崩溃/ANR 保持原有 Error Tracking 跳转不变。
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker


def test_crash_kind_still_uses_error_tracking_url():
    from app.crashguard.api.crash import _datadog_url_for

    url = _datadog_url_for("abc123", window_hours=24, kind="crash", title="SIGABRT", platform="ios")
    assert url.startswith("https://app.datadoghq.com/error-tracking/issue/abc123")
    assert "from_ts=" in url and "to_ts=" in url


def test_empty_kind_defaults_to_error_tracking_url():
    """未标注 kind（legacy 数据）时保持原行为，不误判成 jank。"""
    from app.crashguard.api.crash import _datadog_url_for

    url = _datadog_url_for("abc123", window_hours=24, kind="", title="", platform="")
    assert url.startswith("https://app.datadoghq.com/error-tracking/issue/abc123")


def test_jank_kind_uses_logs_explorer_url_not_error_tracking():
    from app.crashguard.api.crash import _datadog_url_for

    url = _datadog_url_for(
        "jank:14cf9239734ba243", window_hours=24,
        kind="jank", title="Jank @ RootView.commonAlertButtons", platform="ios",
    )
    assert url.startswith("https://app.datadoghq.com/logs?query=")
    assert "error-tracking" not in url
    # 聚合键本身不应该出现在链接里（Datadog 侧根本不认识它）
    assert "14cf9239734ba243" not in url


def test_jank_url_contains_query_platform_and_frame_label():
    from app.crashguard.api.crash import _datadog_url_for
    import urllib.parse as _urlparse

    url = _datadog_url_for(
        "jank:deadbeef", window_hours=24,
        kind="jank", title="Jank @ RootView.commonAlertButtons", platform="ios",
    )
    query_str = url.split("query=", 1)[1].split("&", 1)[0]
    decoded = _urlparse.unquote(query_str)
    assert "jank_watchdog_block" in decoded
    assert "@os.name:ios" in decoded
    assert "RootView.commonAlertButtons" in decoded


def test_jank_url_omits_frame_label_when_placeholder():
    """frame_label 是符号化失败占位符 "?" 时不应该把 "?" 塞进 query（会让搜索无意义地过窄）。"""
    from app.crashguard.api.crash import _datadog_url_for

    url = _datadog_url_for(
        "jank:deadbeef", window_hours=24,
        kind="jank", title="Jank @ ?", platform="android",
    )
    import urllib.parse as _urlparse
    decoded = _urlparse.unquote(url.split("query=", 1)[1].split("&", 1)[0])
    assert '"?"' not in decoded


def test_jank_url_omits_window_params_when_window_hours_zero():
    from app.crashguard.api.crash import _datadog_url_for

    url = _datadog_url_for("jank:deadbeef", window_hours=0, kind="jank", title="Jank @ x", platform="ios")
    assert "from_ts=" not in url and "to_ts=" not in url


@pytest.fixture
async def patched_session(db_engine):
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
async def test_issue_detail_datadog_url_for_jank_issue(patched_session):
    """端到端：get_issue_detail() 对 kind='jank' 的 issue 应返回 Logs Explorer 链接。"""
    from app.crashguard.api.crash import get_issue_detail
    from app.crashguard.models import CrashIssue

    async with patched_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="jank:14cf9239734ba243",
            platform="ios",
            kind="jank",
            fatality="jank",
            title="Jank @ RootView.commonAlertButtons",
            stack_fingerprint="fpjank",
        ))
        await session.commit()

    detail = await get_issue_detail("jank:14cf9239734ba243")
    assert detail["datadog_url"].startswith("https://app.datadoghq.com/logs?query=")
    assert "error-tracking" not in detail["datadog_url"]
