"""jank_ingester.py 单测（2026-07-20）。

覆盖：聚合键计算（compute_jank_aggregation_key）、单条日志解析（_parse_jank_event）、
以及完整摄入循环 ingest_jank_logs()（upsert crash_issues/crash_snapshots + 新 issue
触发符号化 + cursor 持久化 + 分页）。
"""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.crashguard.models  # noqa: F401 — 注册 crash_* 表到 Base.metadata


# ── compute_jank_aggregation_key ─────────────────────────────────────────────

def test_ios_key_uses_module_and_offset():
    from app.crashguard.services.jank_ingester import compute_jank_aggregation_key

    key = compute_jank_aggregation_key(
        platform="ios", has_app_frame=True,
        app_stack_module="Plaud-Global", app_stack_module_offset="0x0000000000e31758",
    )
    assert len(key) == 16
    # 同一偏移必须算出同一个键（同一处卡顿反复出现要落到同一个 issue）
    key2 = compute_jank_aggregation_key(
        platform="ios", has_app_frame=True,
        app_stack_module="Plaud-Global", app_stack_module_offset="0x0000000000e31758",
    )
    assert key == key2


def test_ios_key_differs_for_different_offset():
    from app.crashguard.services.jank_ingester import compute_jank_aggregation_key

    key1 = compute_jank_aggregation_key(
        platform="ios", has_app_frame=True,
        app_stack_module="Plaud-Global", app_stack_module_offset="0x0000000000e31758",
    )
    key2 = compute_jank_aggregation_key(
        platform="ios", has_app_frame=True,
        app_stack_module="Plaud-Global", app_stack_module_offset="0x0000000000f42869",
    )
    assert key1 != key2


def test_ios_key_stable_across_aslr_pc_drift():
    """回归测试：ASLR 导致同一处代码每次启动 app_stack_pc（绝对地址）都不同，
    但 app_stack_module + app_stack_module_offset（相对偏移）必须稳定不变，
    算出同一个聚合键——否则同一处卡顿会被碎片化成多个 issue（102 生产环境实测：
    同一 offset 0x0000000000e31758 对应了 16 个不同的 module_base/pc 组合）。

    compute_jank_aggregation_key() 的签名里已经不再接受 app_stack_pc 参数，
    所以这里直接验证：调用方即便拿到不同的 app_stack_pc，也只会把稳定的
    app_stack_module_offset 传进来参与聚合键计算，二者必然得到同一个键。
    """
    from app.crashguard.services.jank_ingester import compute_jank_aggregation_key

    # 模拟两次不同启动：module_base 不同 → app_stack_pc 不同，但 offset 相同
    pc_boot1 = "0x0000000103e42dd4"  # module_base_1 + 0x0000000000e31758
    pc_boot2 = "0x0000000105cec708"  # module_base_2 + 0x0000000000e31758（不同 module_base）
    assert pc_boot1 != pc_boot2  # 前提：两次启动确实产生了不同的绝对地址

    key_boot1 = compute_jank_aggregation_key(
        platform="ios", has_app_frame=True,
        app_stack_module="Plaud-Global", app_stack_module_offset="0x0000000000e31758",
    )
    key_boot2 = compute_jank_aggregation_key(
        platform="ios", has_app_frame=True,
        app_stack_module="Plaud-Global", app_stack_module_offset="0x0000000000e31758",
    )
    assert key_boot1 == key_boot2


def test_android_key_uses_frame_text():
    from app.crashguard.services.jank_ingester import compute_jank_aggregation_key

    key1 = compute_jank_aggregation_key(
        platform="android", has_app_frame=True,
        app_stack_frame="ai.plaud.android.payment.k.a",
    )
    key2 = compute_jank_aggregation_key(
        platform="android", has_app_frame=True,
        app_stack_frame="ai.plaud.android.payment.k.a",
    )
    key3 = compute_jank_aggregation_key(
        platform="android", has_app_frame=True,
        app_stack_frame="ai.plaud.android.markdown.render.MarkdownViewKt",
    )
    assert key1 == key2
    assert key1 != key3


def test_no_app_frame_uses_top_module_and_symbol_bucket():
    from app.crashguard.services.jank_ingester import compute_jank_aggregation_key

    key1 = compute_jank_aggregation_key(
        platform="ios", has_app_frame=False,
        stack_top_module="QuartzCore", stack_top_symbol="CA::Layer::layout_if_needed",
    )
    key2 = compute_jank_aggregation_key(
        platform="ios", has_app_frame=False,
        stack_top_module="QuartzCore", stack_top_symbol="CA::Layer::layout_if_needed",
    )
    key3 = compute_jank_aggregation_key(
        platform="android", has_app_frame=False,
        stack_top_module="android.os", stack_top_symbol="Handler.dispatchMessage",
    )
    assert key1 == key2
    assert key1 != key3


# ── _parse_jank_event ────────────────────────────────────────────────────────

def _raw_event(attrs: dict) -> dict:
    return {"attributes": {"attributes": attrs}}


def test_parse_ios_event_with_app_frame():
    from app.crashguard.services.jank_ingester import _parse_jank_event

    parsed = _parse_jank_event(_raw_event({
        "os": {"name": "iOS", "version": "26.0.1"},
        "has_app_frame": True,
        "app_stack_module": "Plaud-Global",
        "app_stack_pc": "0x0000000103e42dd4",
        "app_stack_module_offset": "0x0000000000e31758",
        "app_stack_module_base": "0x0000000102f1c000",
        "app_stack_frame": "Plaud-Global ???",
        "stack_top_module": "QuartzCore",
        "stack_top_symbol": "some_symbol",
        "stack_trace": "0   QuartzCore ...",
        "version": "4.0.201-941",
    }))
    assert parsed is not None
    assert parsed["platform"] == "ios"
    assert parsed["has_app_frame"] is True
    assert parsed["frame_label"] == "Plaud-Global"
    assert parsed["issue_id"].startswith("jank:")
    assert parsed["app_version"] == "4.0.201-941"
    assert parsed["app_stack_module_offset"] == "0x0000000000e31758"
    # app_stack_pc 仍然要提取出来（符号化 atos/dSYM 查询要用绝对地址），只是不参与聚合键
    assert parsed["app_stack_pc"] == "0x0000000103e42dd4"


def test_parse_ios_event_issue_id_stable_across_aslr_pc_drift():
    """回归测试：同一 offset、不同 pc/module_base（模拟不同启动的 ASLR 随机化）
    必须解析出同一个 issue_id；不同 offset 必须解析出不同的 issue_id。"""
    from app.crashguard.services.jank_ingester import _parse_jank_event

    base_attrs = {
        "os": {"name": "iOS"},
        "has_app_frame": True,
        "app_stack_module": "Plaud-Global",
        "stack_trace": "0   Plaud-Global ...",
        "version": "4.0.201-941",
    }

    parsed_boot1 = _parse_jank_event(_raw_event({
        **base_attrs,
        "app_stack_pc": "0x0000000103e42dd4",
        "app_stack_module_base": "0x0000000102f1c000",
        "app_stack_module_offset": "0x0000000000e31758",
    }))
    parsed_boot2 = _parse_jank_event(_raw_event({
        **base_attrs,
        "app_stack_pc": "0x0000000105cec708",  # 不同启动，ASLR 导致绝对地址不同
        "app_stack_module_base": "0x0000000104fdb000",  # module_base 也不同
        "app_stack_module_offset": "0x0000000000e31758",  # 但相对偏移相同
    }))
    parsed_different_offset = _parse_jank_event(_raw_event({
        **base_attrs,
        "app_stack_pc": "0x0000000103e42dd4",
        "app_stack_module_base": "0x0000000102f1c000",
        "app_stack_module_offset": "0x0000000000f42869",  # 不同的代码位置
    }))

    assert parsed_boot1["issue_id"] == parsed_boot2["issue_id"]
    assert parsed_boot1["issue_id"] != parsed_different_offset["issue_id"]


def test_parse_android_event_with_app_frame():
    from app.crashguard.services.jank_ingester import _parse_jank_event

    parsed = _parse_jank_event(_raw_event({
        "os": {"name": "Android", "version": "14"},
        "has_app_frame": True,
        "app_stack_frame": "ai.plaud.android.payment.k.a",
        "stack_top_module": "android.os",
        "stack_top_symbol": "Handler.dispatchMessage",
        "stack_trace": "  at ...",
        "version": None,
    }))
    assert parsed is not None
    assert parsed["platform"] == "android"
    assert parsed["frame_label"] == "ai.plaud.android.payment.k.a"


def test_parse_android_event_uses_build_id_as_symbol_key():
    """2026-07-23 生产实测：Android jank 事件没有 @version，只有 @build_id(UUID) +
    @build_version(数字)。符号包按 Datadog 区分构建的 build_id 精确匹配，所以 android 的
    symbol_key 必须取 build_id（不是空的 version），否则 mapping 永远查不到、混淆帧永远解不开。
    display_version 回退 build_version 供 UI 展示。"""
    from app.crashguard.services.jank_ingester import _parse_jank_event

    parsed = _parse_jank_event(_raw_event({
        "os": {"name": "Android", "version": "14"},
        "has_app_frame": True,
        "app_stack_frame": "J.N.M01adZlM",
        "stack_trace": "  at ...",
        "version": None,
        "build_id": "196bae40-11fd-3a88-bfb8-e2dc315b3bbb",
        "build_version": "946",
    }))
    assert parsed is not None
    assert parsed["app_version"] == ""            # android 事件确实没有 @version
    assert parsed["build_id"] == "196bae40-11fd-3a88-bfb8-e2dc315b3bbb"
    assert parsed["symbol_key"] == "196bae40-11fd-3a88-bfb8-e2dc315b3bbb"  # 查符号用 build_id
    assert parsed["display_version"] == "946"     # UI 版本列回退 build_version


def test_parse_ios_event_symbol_key_is_version():
    """iOS jank 事件带 @version、无 build UUID —— symbol_key 取 version（与打包机
    上传 dSYM 时用的 .app CFBundleShortVersion-CFBundleVersion 一致）。"""
    from app.crashguard.services.jank_ingester import _parse_jank_event

    parsed = _parse_jank_event(_raw_event({
        "os": {"name": "iOS", "version": "17.5"},
        "has_app_frame": True,
        "app_stack_module": "Plaud-Global",
        "app_stack_module_offset": "0x1016f562c",
        "app_stack_frame": "Plaud-Global",
        "stack_trace": "0  Plaud-Global ...",
        "version": "4.0.201-945",
        "build_version": "945",
    }))
    assert parsed is not None
    assert parsed["symbol_key"] == "4.0.201-945"
    assert parsed["display_version"] == "4.0.201-945"


def test_parse_event_extracts_page_field():
    """`page` 是 Datadog 卡顿看板按页面分组统计的原生维度（生产环境实测 100% 有
    值），必须被 _parse_jank_event 提取出来，供 _upsert_jank_event 累计 top_page 分布。"""
    from app.crashguard.services.jank_ingester import _parse_jank_event

    parsed = _parse_jank_event(_raw_event({
        "os": {"name": "Android", "version": "14"},
        "has_app_frame": True,
        "app_stack_frame": "ai.plaud.android.payment.k.a",
        "stack_trace": "  at ...",
        "version": "4.0.201-941",
        "page": "fileDetail",
    }))
    assert parsed is not None
    assert parsed["page"] == "fileDetail"


def test_parse_event_missing_page_defaults_to_empty_string():
    from app.crashguard.services.jank_ingester import _parse_jank_event

    parsed = _parse_jank_event(_raw_event({
        "os": {"name": "iOS"},
        "has_app_frame": False,
        "stack_top_module": "QuartzCore",
        "stack_top_symbol": "CA::Layer::layout_if_needed",
    }))
    assert parsed is not None
    assert parsed["page"] == ""


def test_parse_event_without_app_frame_falls_back_to_top_symbol():
    from app.crashguard.services.jank_ingester import _parse_jank_event

    parsed = _parse_jank_event(_raw_event({
        "os": {"name": "iOS"},
        "has_app_frame": False,
        "stack_top_module": "QuartzCore",
        "stack_top_symbol": "CA::Layer::layout_if_needed",
    }))
    assert parsed is not None
    assert parsed["has_app_frame"] is False
    assert parsed["frame_label"] == "QuartzCore::CA::Layer::layout_if_needed"


def test_parse_event_missing_os_returns_none():
    from app.crashguard.services.jank_ingester import _parse_jank_event

    assert _parse_jank_event(_raw_event({"has_app_frame": True})) is None
    assert _parse_jank_event({}) is None
    assert _parse_jank_event(_raw_event({"os": {"name": ""}})) is None


# ── ingest_jank_logs（完整摄入循环） ──────────────────────────────────────────

@pytest.fixture
async def patched_session(db_engine):
    import app.db.database as db_mod

    async with db_engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.create_all)

    original = db_mod._session_factory
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    db_mod._session_factory = factory
    yield factory
    db_mod._session_factory = original


def _patch_settings(monkeypatch):
    s = MagicMock()
    s.datadog_api_key = "fake-key"
    s.datadog_app_key = "fake-app-key"
    s.datadog_site = "datadoghq.com"
    s.datadog_service_filter = ""
    monkeypatch.setattr(
        "app.crashguard.services.jank_ingester.get_crashguard_settings", lambda: s,
    )
    return s


_FAKE_FULL_STACK = (
    "0   Plaud-Global   SomeClass.someMethod\n"
    "1   Foundation   0x0000000182ff006c   0x0000000182fc7000 + 165996\n"
)


def _patch_no_op_symbolication_deps(monkeypatch):
    """符号化路径不是本测试重点：resolve/symbolicate 都 no-op，只验证摄入/upsert 逻辑。"""
    monkeypatch.setattr(
        "app.crashguard.services.symbolication.symbolicate_jank_frame",
        AsyncMock(return_value="SomeClass.someMethod"),
    )
    monkeypatch.setattr(
        "app.crashguard.services.symbolication.symbolicate_jank_stack",
        AsyncMock(return_value=_FAKE_FULL_STACK),
    )
    monkeypatch.setattr("app.config.get_repo_routing", lambda: {})
    monkeypatch.setattr(
        "app.services.repo_router.resolve",
        lambda platform, version, routing: None,
    )


@pytest.mark.asyncio
async def test_ingest_creates_new_fixable_issue_and_symbolicates(patched_session, monkeypatch):
    from app.crashguard.services.jank_ingester import ingest_jank_logs
    from app.crashguard.models import CrashIssue, CrashSnapshot
    from app.db.database import get_session
    from sqlalchemy import select

    _patch_settings(monkeypatch)
    _patch_no_op_symbolication_deps(monkeypatch)

    event = _raw_event({
        "os": {"name": "iOS"},
        "has_app_frame": True,
        "app_stack_module": "Plaud-Global",
        "app_stack_pc": "0x0000000103e42dd4",
        "app_stack_module_offset": "0x0000000000e31758",
        "app_stack_module_base": "0x0000000102f1c000",
        "stack_trace": "0   Plaud-Global 0x... + 123",
        "version": "4.0.201-941",
    })
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page",
        AsyncMock(return_value={"data": [event], "next_cursor": None}),
    )

    now = datetime(2026, 7, 20, 12, 0, 0)
    result = await ingest_jank_logs(now=now)

    assert result == {"scanned": 1, "new_issues": 1, "updated_issues": 0}

    async with get_session() as s:
        issue = (await s.execute(select(CrashIssue))).scalar_one()
        snap = (await s.execute(select(CrashSnapshot))).scalar_one()

    assert issue.kind == "jank"
    assert issue.fatality == "jank"
    assert issue.fixable is True
    assert issue.platform == "ios"
    assert issue.total_events == 1
    # 完整多帧堆栈符号化结果写回 representative_stack（不是单帧覆盖）
    assert issue.representative_stack == _FAKE_FULL_STACK
    assert issue.representative_stack.count("\n") > 1
    # 单帧符号化结果非占位符 → 标题回写为可读函数名
    assert issue.title == "Jank @ SomeClass.someMethod"
    assert issue.prewarm_attempts == 1
    assert snap.events_count == 1
    assert snap.snapshot_date == date(2026, 7, 20)


@pytest.mark.asyncio
async def test_ingest_marks_no_app_frame_issue_unfixable_and_skips_symbolication(
    patched_session, monkeypatch,
):
    from app.crashguard.services.jank_ingester import ingest_jank_logs
    from app.crashguard.models import CrashIssue
    from app.db.database import get_session
    from sqlalchemy import select

    _patch_settings(monkeypatch)
    symbolicate_mock = AsyncMock(return_value="should not be called")
    monkeypatch.setattr(
        "app.crashguard.services.symbolication.symbolicate_jank_frame", symbolicate_mock,
    )

    event = _raw_event({
        "os": {"name": "ios"},
        "has_app_frame": False,
        "stack_top_module": "QuartzCore",
        "stack_top_symbol": "layout",
        "stack_trace": "0   QuartzCore 0x... + 1",
        "version": "4.0.201-941",
    })
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page",
        AsyncMock(return_value={"data": [event], "next_cursor": None}),
    )

    await ingest_jank_logs(now=datetime(2026, 7, 20, 12, 0, 0))

    async with get_session() as s:
        issue = (await s.execute(select(CrashIssue))).scalar_one()
    assert issue.fixable is False
    symbolicate_mock.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_increments_existing_issue_and_snapshot(patched_session, monkeypatch):
    from app.crashguard.services.jank_ingester import ingest_jank_logs
    from app.crashguard.models import CrashIssue, CrashSnapshot
    from app.db.database import get_session
    from sqlalchemy import select

    _patch_settings(monkeypatch)
    _patch_no_op_symbolication_deps(monkeypatch)

    event = _raw_event({
        "os": {"name": "Android"},
        "has_app_frame": True,
        "app_stack_frame": "ai.plaud.android.payment.k.a",
        "stack_trace": "  at ...",
        "version": "4.0.201-941",
    })
    search_mock = AsyncMock(return_value={"data": [event], "next_cursor": None})
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page", search_mock,
    )

    # 第一次摄入：新建
    await ingest_jank_logs(now=datetime(2026, 7, 20, 8, 0, 0))
    # 第二次摄入（同一天，同一处卡顿再次出现）：应该累加而不是新建
    await ingest_jank_logs(now=datetime(2026, 7, 20, 9, 0, 0))

    async with get_session() as s:
        issues = (await s.execute(select(CrashIssue))).scalars().all()
        snaps = (await s.execute(select(CrashSnapshot))).scalars().all()

    assert len(issues) == 1
    assert issues[0].total_events == 2
    assert len(snaps) == 1
    assert snaps[0].events_count == 2


@pytest.mark.asyncio
async def test_ingest_accumulates_top_page_distribution_across_events(patched_session, monkeypatch):
    """3 条同一聚合键（同 app_stack_frame）、不同 page 的事件依次摄入后，issue.top_page
    应该按出现频次排序，且百分比精确匹配 round(count/total*100, 1)（2/3 → 66.7%）。"""
    from app.crashguard.services.jank_ingester import ingest_jank_logs
    from app.crashguard.models import CrashIssue
    from app.db.database import get_session
    from sqlalchemy import select

    _patch_settings(monkeypatch)
    _patch_no_op_symbolication_deps(monkeypatch)

    def _event(page: str) -> dict:
        return _raw_event({
            "os": {"name": "Android"},
            "has_app_frame": True,
            "app_stack_frame": "ai.plaud.android.payment.k.a",
            "stack_trace": "  at ...",
            "version": "4.0.201-941",
            "page": page,
        })

    search_mock = AsyncMock(side_effect=[
        {"data": [_event("home"), _event("home")], "next_cursor": None},
        {"data": [_event("login")], "next_cursor": None},
    ])
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page", search_mock,
    )

    await ingest_jank_logs(now=datetime(2026, 7, 20, 8, 0, 0))
    await ingest_jank_logs(now=datetime(2026, 7, 20, 9, 0, 0))

    async with get_session() as s:
        issue = (await s.execute(select(CrashIssue))).scalar_one()

    expected_home_pct = round(2 / 3 * 100, 1)
    assert issue.top_page == f"home ({expected_home_pct}%), login (33.3%)"
    # tags.page_counts 是底层持久化的原始计数，独立于格式化后的 top_page 字符串
    import json
    tags = json.loads(issue.tags)
    assert tags["page_counts"] == {"home": 2, "login": 1}


@pytest.mark.asyncio
async def test_ingest_paginates_until_no_next_cursor(patched_session, monkeypatch):
    from app.crashguard.services.jank_ingester import ingest_jank_logs
    from app.crashguard.models import CrashIssue
    from app.db.database import get_session
    from sqlalchemy import select

    _patch_settings(monkeypatch)
    _patch_no_op_symbolication_deps(monkeypatch)

    def _event(offset: str, pc: str = "0x1") -> dict:
        return _raw_event({
            "os": {"name": "iOS"}, "has_app_frame": True,
            "app_stack_module": "Plaud-Global", "app_stack_pc": pc,
            "app_stack_module_offset": offset,
            "app_stack_module_base": "0x0", "version": "4.0.0-1",
        })

    # 第一页：两个不同 offset（两处不同的卡顿代码位置）→ 两个不同 issue
    # 第二页：offset 和第一页第一条相同，但 pc（绝对地址，模拟不同启动/ASLR）不同
    # → 必须命中已有 issue 而不是新建（这是本次 offset-based 聚合修复的核心行为）
    page1 = {
        "data": [_event("0x0000000000e31758", pc="0x0000000103e42dd4"),
                 _event("0x0000000000f42869", pc="0x0000000103e42dd5")],
        "next_cursor": "cursor-2",
    }
    page2 = {
        "data": [_event("0x0000000000e31758", pc="0x0000000105cec708")],
        "next_cursor": None,
    }
    search_mock = AsyncMock(side_effect=[page1, page2])
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page", search_mock,
    )

    result = await ingest_jank_logs(now=datetime(2026, 7, 20, 12, 0, 0))

    assert result["scanned"] == 3
    assert result["new_issues"] == 2   # 两个不同 offset → 两个不同 issue
    assert result["updated_issues"] == 1  # 第二页复用 offset → 命中已有 issue，即便 pc 不同
    assert search_mock.call_count == 2
    # 第二次调用应该带上第一页返回的 cursor
    assert search_mock.call_args_list[1].kwargs["cursor"] == "cursor-2"

    async with get_session() as s:
        count = len((await s.execute(select(CrashIssue))).scalars().all())
    assert count == 2


@pytest.mark.asyncio
async def test_ingest_persists_and_reuses_cursor(patched_session, monkeypatch):
    from app.crashguard.services.jank_ingester import ingest_jank_logs

    _patch_settings(monkeypatch)
    _patch_no_op_symbolication_deps(monkeypatch)

    search_mock = AsyncMock(return_value={"data": [], "next_cursor": None})
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page", search_mock,
    )

    from datetime import timezone as _tz

    first_now = datetime(2026, 7, 20, 12, 0, 0)
    await ingest_jank_logs(now=first_now)
    first_call_from_ms = search_mock.call_args_list[0].kwargs["from_ms"]
    # 首次运行无历史 cursor，应该用默认回看窗口。now 是 naive datetime 代表 UTC 时刻，
    # 必须显式标注 tzinfo=utc 再 .timestamp()，否则在非 UTC 时区的机器上跑测试会算错
    # （这正是 2026-07-21 生产环境 TZ=Asia/Shanghai 上发现的 8 小时窗口偏移 bug）。
    expected_first_from = int(first_now.replace(tzinfo=_tz.utc).timestamp() * 1000) - 4 * 3600 * 1000
    assert first_call_from_ms == expected_first_from

    second_now = datetime(2026, 7, 20, 16, 0, 0)
    await ingest_jank_logs(now=second_now)
    second_call_from_ms = search_mock.call_args_list[1].kwargs["from_ms"]
    # 第二次应该复用第一次的 to_ms 作为 from_ms（cursor 持久化），而不是重新回看4h
    assert second_call_from_ms == int(first_now.replace(tzinfo=_tz.utc).timestamp() * 1000)


@pytest.mark.asyncio
async def test_ingest_to_ms_independent_of_local_timezone(patched_session, monkeypatch):
    """回归测试（2026-07-21 生产环境实测发现）：容器 TZ=Asia/Shanghai 时，旧实现
    `int(now.timestamp() * 1000)` 对 naive datetime 按本地时区解释，算出的 to_ms
    比真实 UTC 时刻提前 8 小时，导致最近 8 小时的卡顿事件被排除在查询窗口外。
    用 calendar.timegm()（不受本地时区影响的独立 oracle）交叉验证 to_ms 正确。
    """
    import calendar
    import os
    import time as _time

    from app.crashguard.services.jank_ingester import ingest_jank_logs

    _patch_settings(monkeypatch)
    _patch_no_op_symbolication_deps(monkeypatch)

    search_mock = AsyncMock(return_value={"data": [], "next_cursor": None})
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page", search_mock,
    )

    now = datetime(2026, 7, 20, 12, 0, 0)
    expected_to_ms = calendar.timegm(now.timetuple()) * 1000  # TZ 无关的 UTC epoch oracle

    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    _time.tzset()
    try:
        await ingest_jank_logs(now=now)
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        _time.tzset()

    actual_to_ms = search_mock.call_args_list[0].kwargs["to_ms"]
    assert actual_to_ms == expected_to_ms


@pytest.mark.asyncio
async def test_ingest_skips_when_datadog_key_missing(patched_session, monkeypatch):
    from app.crashguard.services.jank_ingester import ingest_jank_logs

    s = MagicMock()
    s.datadog_api_key = ""
    monkeypatch.setattr(
        "app.crashguard.services.jank_ingester.get_crashguard_settings", lambda: s,
    )
    search_mock = AsyncMock()
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page", search_mock,
    )

    result = await ingest_jank_logs()
    assert result == {"scanned": 0, "new_issues": 0, "updated_issues": 0}
    search_mock.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_continues_when_symbolication_raises(patched_session, monkeypatch):
    """符号化异常不能中断整个摄入循环——issue 照样建，只是符号化失败记录下来。"""
    from app.crashguard.services.jank_ingester import ingest_jank_logs
    from app.crashguard.models import CrashIssue
    from app.db.database import get_session
    from sqlalchemy import select

    _patch_settings(monkeypatch)
    monkeypatch.setattr("app.config.get_repo_routing", lambda: {})
    monkeypatch.setattr(
        "app.services.repo_router.resolve", lambda platform, version, routing: None,
    )

    async def _boom(**kwargs):
        raise RuntimeError("symbol package download exploded")

    monkeypatch.setattr(
        "app.crashguard.services.symbolication.symbolicate_jank_frame", _boom,
    )

    event = _raw_event({
        "os": {"name": "iOS"}, "has_app_frame": True,
        "app_stack_module": "Plaud-Global", "app_stack_pc": "0x1",
        "app_stack_module_offset": "0x0000000000e31758",
        "app_stack_module_base": "0x0", "version": "4.0.0-1",
    })
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page",
        AsyncMock(return_value={"data": [event], "next_cursor": None}),
    )

    result = await ingest_jank_logs(now=datetime(2026, 7, 20, 12, 0, 0))
    assert result["new_issues"] == 1

    async with get_session() as s:
        issue = (await s.execute(select(CrashIssue))).scalar_one()
    assert issue.prewarm_attempts == 1
    assert "symbol package download exploded" in issue.prewarm_last_error
    # representative_stack 保留摄入时的原始占位（未被符号化覆盖）
    assert issue.representative_stack == ""


@pytest.mark.asyncio
async def test_ingest_keeps_placeholder_title_and_stack_when_symbolication_fails(
    patched_session, monkeypatch,
):
    """单帧符号化返回占位符（未命中 dSYM，模拟失败态，但不抛异常）→ 标题保留摄入时
    原始占位（"Jank @ Plaud-Global"），representative_stack 保留原始 stack_trace
    （不是空、不是报错、也不会被占位符覆盖）——这是本次修复的最低要求。"""
    from app.crashguard.services.jank_ingester import ingest_jank_logs
    from app.crashguard.models import CrashIssue
    from app.db.database import get_session
    from sqlalchemy import select

    _patch_settings(monkeypatch)
    monkeypatch.setattr("app.config.get_repo_routing", lambda: {})
    monkeypatch.setattr(
        "app.services.repo_router.resolve", lambda platform, version, routing: None,
    )

    original_stack_trace = "0   Plaud-Global 0x0000000103e42dd4 0x0000000102f1c000 + 15887828\n"

    # 单帧符号化未命中 dSYM → 占位符（不抛异常）
    monkeypatch.setattr(
        "app.crashguard.services.symbolication.symbolicate_jank_frame",
        AsyncMock(return_value="Plaud-Global + 0x0000000103e42dd4"),
    )
    # 完整堆栈符号化同样未命中 → 原样返回传入的 stack_trace（模拟 symbolicate_jank_stack
    # 自身的失败降级行为，而不是真的调用它）
    monkeypatch.setattr(
        "app.crashguard.services.symbolication.symbolicate_jank_stack",
        AsyncMock(return_value=original_stack_trace),
    )

    event = _raw_event({
        "os": {"name": "iOS"}, "has_app_frame": True,
        "app_stack_module": "Plaud-Global", "app_stack_pc": "0x0000000103e42dd4",
        "app_stack_module_offset": "0x0000000000e31758",
        "app_stack_module_base": "0x0000000102f1c000",
        "stack_trace": original_stack_trace,
        "version": "4.0.201-941",
    })
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page",
        AsyncMock(return_value={"data": [event], "next_cursor": None}),
    )

    await ingest_jank_logs(now=datetime(2026, 7, 20, 12, 0, 0))

    async with get_session() as s:
        issue = (await s.execute(select(CrashIssue))).scalar_one()

    # 摄入时建 issue 用的原始占位标题（frame_label = module 名）保持不变
    assert issue.title == "Jank @ Plaud-Global"
    # representative_stack 保留原始完整 stack_trace，不是空、不是报错
    assert issue.representative_stack == original_stack_trace
    assert issue.prewarm_attempts == 1
    assert issue.prewarm_last_error == ""
