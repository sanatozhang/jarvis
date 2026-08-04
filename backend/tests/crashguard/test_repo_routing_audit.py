"""A4: repo_router.resolve() 返回 None 时的 4 个调用点补写审计记录（2026-08-04，批次 3）。

背景：调用方现状是完全静默兜底（只有 logger.warning，没有任何落库/可查询的记录）。
本测试覆盖 4 个调用点，各构造一个 resolve() 返回 None 的场景，断言
`app.crashguard.services.audit.write_audit` 被调用且 op="repo_routing_unresolved"，
detail 里含预期的 platform/app_version/caller 字段：

1. `app.crashguard.workers.pipeline._try_symbolicate_issue`
2. `app.crashguard.services.datadog_client.DatadogClient.get_issue_detail`
3. `app.crashguard.services.jank_ingester._symbolicate_new_jank_issue`
4. `app.crashguard.services.pr_drafter.draft_pr_for_analysis`
"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.crashguard.models  # noqa: F401 — 注册 crash_* 表


@pytest.fixture
async def patched_session(db_engine):
    """复用 conftest db_engine，把全局 _session_factory 指过来。"""
    import app.db.database as db_mod

    async with db_engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.create_all)

    original = db_mod._session_factory
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    db_mod._session_factory = factory
    yield factory
    db_mod._session_factory = original


# ---------------------------------------------------------------------------
# 1. pipeline._try_symbolicate_issue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pipeline_try_symbolicate_issue_writes_audit_when_resolve_none(
    patched_session, monkeypatch,
):
    from app.crashguard.workers.pipeline import _try_symbolicate_issue
    from app.crashguard.models import CrashIssue
    from app.db.database import get_session

    issue_id = "dd-issue-audit-1"
    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id=issue_id, platform="android",
            representative_stack="original stack",
            last_seen_version="4.1.0-720",
        ))
        await session.commit()

    monkeypatch.setattr("app.config.get_repo_routing", lambda: {})
    monkeypatch.setattr("app.services.repo_router.resolve", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.crashguard.services.symbolication.symbolicate_stack",
        AsyncMock(return_value="original stack"),
    )
    audit_mock = AsyncMock()
    monkeypatch.setattr("app.crashguard.services.audit.write_audit", audit_mock)

    await _try_symbolicate_issue(issue_id, "android")

    audit_mock.assert_called_once()
    kwargs = audit_mock.call_args.kwargs
    assert kwargs["op"] == "repo_routing_unresolved"
    assert kwargs["target_id"] == issue_id
    assert kwargs["success"] is False
    assert kwargs["detail"]["platform"] == "android"
    assert kwargs["detail"]["app_version"] == "4.1.0-720"
    assert kwargs["detail"]["caller"] == "pipeline._try_symbolicate_issue"


# ---------------------------------------------------------------------------
# 1b. pipeline._try_symbolicate_issue — os_name 清洗口径回归测试（最终修复轮问题 4）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pipeline_try_symbolicate_issue_cleans_top_os_before_resolve(
    patched_session, monkeypatch,
):
    """pipeline.py::_try_symbolicate_issue 传给 repo_router.resolve 的 os_name 必须
    是 `_first_dominant_value` 清洗后的单值（跟 pr_drafter.py 三处同款口径一致），
    不能是 row.top_os 的原始整串——否则 Flutter 跨端聚合的 issue（如
    "iOS 17 (90%), Android 14 (10%)"）会因为串里同时出现 "Android" 字样，被
    `repo_router.normalize_platform`（先判 android 再判 ios）误判成 android 平台，
    对一个实际以 iOS 为主的崩溃是路由错配。"""
    from app.crashguard.workers.pipeline import _try_symbolicate_issue
    from app.crashguard.models import CrashIssue
    from app.db.database import get_session

    issue_id = "dd-issue-os-clean-1"
    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id=issue_id, platform="flutter",
            representative_stack="original stack",
            last_seen_version="3.16.0-634",
            top_os="iOS 17 (90%), Android 14 (10%)",
        ))
        await session.commit()

    captured: dict = {}

    def _fake_resolve(*args, **kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr("app.config.get_repo_routing", lambda: {})
    monkeypatch.setattr("app.services.repo_router.resolve", _fake_resolve)
    monkeypatch.setattr(
        "app.crashguard.services.symbolication.symbolicate_stack",
        AsyncMock(return_value="original stack"),
    )
    monkeypatch.setattr("app.crashguard.services.audit.write_audit", AsyncMock())

    await _try_symbolicate_issue(issue_id, "flutter")

    assert captured.get("os_name") == "iOS 17"


# ---------------------------------------------------------------------------
# 2. datadog_client.DatadogClient.get_issue_detail
# ---------------------------------------------------------------------------

def _make_rum_event(stack: str, *, os_name: str = "ios", app_version: str = "3.18.0-708") -> SimpleNamespace:
    inner = {
        "error": {"stack": stack, "binary_images": []},
        "os": {"name": os_name},
        "application": {"version": app_version},
    }
    return SimpleNamespace(
        attributes=SimpleNamespace(attributes=inner, _data_store={}, timestamp=1700000000000),
    )


@pytest.mark.asyncio
async def test_get_issue_detail_writes_audit_when_resolve_none(monkeypatch):
    from app.crashguard.services.datadog_client import DatadogClient

    raw_stack = "0   App   0x0000000112fec700 0x11214c000 + 15337216"
    event = _make_rum_event(raw_stack)

    client = DatadogClient(api_key="x", app_key="y")
    monkeypatch.setattr(DatadogClient, "_sync_search_rum_events", lambda self, *a, **k: [event])
    monkeypatch.setattr("app.config.get_repo_routing", lambda: {})
    monkeypatch.setattr("app.services.repo_router.resolve", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.crashguard.services.symbolication.symbolicate_stack",
        AsyncMock(return_value=raw_stack),
    )
    audit_mock = AsyncMock()
    monkeypatch.setattr("app.crashguard.services.audit.write_audit", audit_mock)

    detail = await client.get_issue_detail("issue-audit-2")

    assert detail is not None
    audit_mock.assert_called_once()
    kwargs = audit_mock.call_args.kwargs
    assert kwargs["op"] == "repo_routing_unresolved"
    assert kwargs["target_id"] == "issue-audit-2"
    assert kwargs["success"] is False
    assert kwargs["detail"]["platform"] == "ios"
    assert kwargs["detail"]["app_version"] == "3.18.0-708"
    assert kwargs["detail"]["caller"] == "datadog_client.get_issue_detail"


# ---------------------------------------------------------------------------
# 3. jank_ingester._symbolicate_new_jank_issue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_symbolicate_new_jank_issue_writes_audit_when_resolve_none(
    patched_session, monkeypatch,
):
    from app.crashguard.services.jank_ingester import _symbolicate_new_jank_issue

    parsed = {
        "platform": "ios", "app_version": "4.1.0-720", "symbol_key": "4.1.0-720",
        "app_stack_module": "Plaud-Global", "app_stack_frame": "0x1", "app_stack_pc": "0x1",
        "app_stack_module_base": "0x0",
    }
    monkeypatch.setattr("app.config.get_repo_routing", lambda: {})
    monkeypatch.setattr("app.services.repo_router.resolve", lambda *a, **k: None)
    # symbolicate_jank_frame 抛异常让函数在早期 return（我们只关心 audit 是否写入，
    # 不需要跑完整个符号化+DB 回写流程）。
    monkeypatch.setattr(
        "app.crashguard.services.symbolication.symbolicate_jank_frame",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    audit_mock = AsyncMock()
    monkeypatch.setattr("app.crashguard.services.audit.write_audit", audit_mock)

    await _symbolicate_new_jank_issue("jank:issue-audit-3", parsed)

    audit_mock.assert_called_once()
    kwargs = audit_mock.call_args.kwargs
    assert kwargs["op"] == "repo_routing_unresolved"
    assert kwargs["target_id"] == "jank:issue-audit-3"
    assert kwargs["success"] is False
    assert kwargs["detail"]["platform"] == "ios"
    assert kwargs["detail"]["app_version"] == "4.1.0-720"
    assert kwargs["detail"]["caller"] == "jank_ingester"


# ---------------------------------------------------------------------------
# 4. pr_drafter.draft_pr_for_analysis
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_draft_pr_for_analysis_writes_audit_when_resolve_none(tmp_path, monkeypatch):
    from app.crashguard.services import pr_drafter

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'audit_test_4.db'}")
    from app.db.database import get_session, init_db
    from app.crashguard import models  # noqa: F401
    from app.crashguard.models import CrashAnalysis, CrashIssue

    await init_db()
    issue_id = "ddi_audit_test_4"
    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id=issue_id,
            platform="weirdplatform",
            title="crash",
            top_app_version="1.0.0",
        ))
        ana = CrashAnalysis(
            datadog_issue_id=issue_id,
            analysis_run_id=f"run-{issue_id}",
            status="success",
            followup_question="",
            root_cause="root",
            fix_suggestion="fix something",
            feasibility_score=0.9,
        )
        session.add(ana)
        await session.commit()
        analysis_id = ana.id

    monkeypatch.setattr(pr_drafter, "get_repo_routing", lambda: {})
    monkeypatch.setattr(pr_drafter.repo_router, "resolve", lambda *a, **k: None)
    monkeypatch.setattr(
        pr_drafter, "get_crashguard_settings",
        lambda: type("S", (), {
            "pr_enabled": True,
            "scheduler_enabled": True,
            "pr_dedup_days": 30,
            "repo_path_flutter": "", "repo_path_android": "", "repo_path_ios": "",
        })(),
    )
    # _platform_repo_path 兜底走 get_code_repo_for_platform；强制返回空，逼函数在
    # repo_path 检查处早退（不触发真实 git/gh 调用）。
    monkeypatch.setattr("app.config.get_code_repo_for_platform", lambda *a, **k: "")

    audit_mock = AsyncMock()
    monkeypatch.setattr("app.crashguard.services.audit.write_audit", audit_mock)

    result = await pr_drafter.draft_pr_for_analysis(analysis_id, approver="human")

    assert result["ok"] is False
    audit_mock.assert_called_once()
    kwargs = audit_mock.call_args.kwargs
    assert kwargs["op"] == "repo_routing_unresolved"
    assert kwargs["target_id"] == issue_id
    assert kwargs["success"] is False
    assert kwargs["detail"]["platform"] == "weirdplatform"
    assert kwargs["detail"]["app_version"] == "1.0.0"
    assert kwargs["detail"]["caller"] == "pr_drafter.draft_pr_for_analysis"
