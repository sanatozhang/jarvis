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


def test_jank_url_contains_query_platform_and_raw_datadog_attrs():
    """2026-07-24：query 里的帧过滤必须用摄入时落库的原始 Datadog 字段
    （dd_query_attrs），不能再用 title 里符号化后才产生的衍生文本——那段文本
    从未出现在 Datadog 原始日志里，全文短语搜索必然搜不到（真实 case：
    jank:23a3eeec7986072c 的 title 被符号化成一个 Swift mangled witness 符号，
    塞进 query 后完全查不到任何日志）。
    """
    from app.crashguard.api.crash import _datadog_url_for
    import urllib.parse as _urlparse

    url = _datadog_url_for(
        "jank:deadbeef", window_hours=24,
        kind="jank", title="Jank @ RootView.commonAlertButtons", platform="ios",
        dd_query_attrs={"app_stack_module": "RootView", "app_stack_module_offset": "0x1234"},
    )
    query_str = url.split("query=", 1)[1].split("&", 1)[0]
    decoded = _urlparse.unquote(query_str)
    assert "jank_watchdog_block" in decoded
    # Datadog `@os.name:` 精确匹配大小写敏感，真实值是 iOS（非小写 ios）
    assert "@os.name:iOS" in decoded
    assert '@app_stack_module:"RootView"' in decoded
    assert '@app_stack_module_offset:"0x1234"' in decoded
    # title 里符号化后的衍生文本不应该出现在 query 里
    assert "RootView.commonAlertButtons" not in decoded


def test_jank_url_omits_frame_filter_when_dd_query_attrs_missing():
    """老 issue（摄入于本次修复之前）tags 里没有 dd_query_attrs 时，优雅回退成
    不带帧过滤的基础 query，而不是拼一段永远搜不到的 title 短语。"""
    from app.crashguard.api.crash import _datadog_url_for

    url = _datadog_url_for(
        "jank:deadbeef", window_hours=24,
        kind="jank", title="Jank @ ?", platform="android",
    )
    import urllib.parse as _urlparse
    decoded = _urlparse.unquote(url.split("query=", 1)[1].split("&", 1)[0])
    assert "@app_stack_module" not in decoded
    assert "@app_stack_frame" not in decoded
    assert '"?"' not in decoded


def test_jank_url_omits_window_params_when_window_hours_zero():
    from app.crashguard.api.crash import _datadog_url_for

    url = _datadog_url_for("jank:deadbeef", window_hours=0, kind="jank", title="Jank @ x", platform="ios")
    assert "from_ts=" not in url and "to_ts=" not in url


def test_jank_url_os_name_uses_datadog_real_casing_ios():
    """Datadog `@os.name:` 精确匹配大小写敏感——真实值是 iOS，不是 ios。"""
    from app.crashguard.api.crash import _datadog_url_for
    import urllib.parse as _urlparse

    url = _datadog_url_for("jank:deadbeef", window_hours=24, kind="jank", title="", platform="ios")
    decoded = _urlparse.unquote(url.split("query=", 1)[1].split("&", 1)[0])
    assert "@os.name:iOS" in decoded
    assert "@os.name:ios" not in decoded


def test_jank_url_os_name_uses_datadog_real_casing_ipados():
    from app.crashguard.api.crash import _datadog_url_for
    import urllib.parse as _urlparse

    url = _datadog_url_for("jank:deadbeef", window_hours=24, kind="jank", title="", platform="ipados")
    decoded = _urlparse.unquote(url.split("query=", 1)[1].split("&", 1)[0])
    assert "@os.name:iPadOS" in decoded
    assert "@os.name:ipados" not in decoded


def test_jank_url_os_name_uses_datadog_real_casing_android():
    from app.crashguard.api.crash import _datadog_url_for
    import urllib.parse as _urlparse

    url = _datadog_url_for("jank:deadbeef", window_hours=24, kind="jank", title="", platform="android")
    decoded = _urlparse.unquote(url.split("query=", 1)[1].split("&", 1)[0])
    assert "@os.name:Android" in decoded
    assert "@os.name:android" not in decoded


@pytest.mark.parametrize("platform", ["", "flutter"])
def test_jank_url_omits_os_name_filter_for_unknown_platform(platform):
    """空字符串或映射表未覆盖的平台（如 flutter）：不加 @os.name 过滤，
    宁可返回更宽泛的结果也不要因为大小写猜错/映射表没覆盖到而 0 命中。"""
    from app.crashguard.api.crash import _datadog_url_for
    import urllib.parse as _urlparse

    url = _datadog_url_for("jank:deadbeef", window_hours=24, kind="jank", title="", platform=platform)
    decoded = _urlparse.unquote(url.split("query=", 1)[1].split("&", 1)[0])
    assert "@os.name" not in decoded


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
