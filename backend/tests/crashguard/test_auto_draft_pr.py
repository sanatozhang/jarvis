"""Auto draft-PR hook（analyzer._maybe_auto_draft_pr）阈值与失败路径单测"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_skip_when_pr_disabled():
    """pr_enabled=False → 直接返回，不调 draft_pr，不写 audit"""
    from app.crashguard.services import analyzer

    fake_settings = type("S", (), {
        "pr_enabled": False,
        "feasibility_pr_threshold": 0.7,
    })()
    with patch.object(analyzer, "get_crashguard_settings", return_value=fake_settings, create=True), \
         patch("app.crashguard.services.audit.write_audit", new_callable=AsyncMock) as audit_mock, \
         patch("app.crashguard.services.pr_drafter.draft_pr_for_analysis",
               new_callable=AsyncMock) as draft_mock:
        # 注意：fake settings 通过 import 路径替换
        import app.crashguard.config as cfg
        with patch.object(cfg, "get_crashguard_settings", return_value=fake_settings):
            await analyzer._maybe_auto_draft_pr(analysis_id=42, feasibility=0.95)

    audit_mock.assert_not_called()
    draft_mock.assert_not_called()


@pytest.mark.asyncio
async def test_skip_when_below_threshold_writes_audit():
    """feasibility < threshold → 写 audit (below_threshold)，不调 draft_pr"""
    from app.crashguard.services import analyzer
    import app.crashguard.config as cfg

    fake_settings = type("S", (), {
        "pr_enabled": True,
        "feasibility_pr_threshold": 0.7,
    })()
    with patch.object(cfg, "get_crashguard_settings", return_value=fake_settings), \
         patch("app.crashguard.services.audit.write_audit", new_callable=AsyncMock) as audit_mock, \
         patch("app.crashguard.services.pr_drafter.draft_pr_for_analysis",
               new_callable=AsyncMock) as draft_mock:
        await analyzer._maybe_auto_draft_pr(analysis_id=42, feasibility=0.5)

    draft_mock.assert_not_called()
    audit_mock.assert_called_once()
    kwargs = audit_mock.call_args.kwargs
    assert kwargs["op"] == "auto_draft_pr"
    assert kwargs["success"] is False
    assert kwargs["error"] == "below_threshold"


@pytest.mark.asyncio
async def test_invokes_draft_pr_and_records_success():
    """feasibility >= threshold → 调 draft_pr 并写 audit (ok=True)"""
    from app.crashguard.services import analyzer
    import app.crashguard.config as cfg

    fake_settings = type("S", (), {
        "pr_enabled": True,
        "feasibility_pr_threshold": 0.7,
    })()
    fake_result = {"ok": True, "pr_url": "https://example.com/pr/1", "branch_name": "crashguard/ios/abc-202604"}
    with patch.object(cfg, "get_crashguard_settings", return_value=fake_settings), \
         patch("app.crashguard.services.audit.write_audit", new_callable=AsyncMock) as audit_mock, \
         patch("app.crashguard.services.pr_drafter.draft_pr_for_analysis",
               new_callable=AsyncMock, return_value=fake_result) as draft_mock:
        await analyzer._maybe_auto_draft_pr(analysis_id=42, feasibility=0.85)

    draft_mock.assert_awaited_once_with(42, approver="auto")
    audit_mock.assert_called_once()
    kwargs = audit_mock.call_args.kwargs
    assert kwargs["op"] == "auto_draft_pr"
    assert kwargs["success"] is True


@pytest.mark.asyncio
async def test_records_failure_when_draft_pr_returns_error():
    """draft_pr 返回 ok=False（如 dup） → audit 记录 error 字段"""
    from app.crashguard.services import analyzer
    import app.crashguard.config as cfg

    fake_settings = type("S", (), {
        "pr_enabled": True,
        "feasibility_pr_threshold": 0.7,
    })()
    fake_result = {"ok": False, "error": "dup_within_30d"}
    with patch.object(cfg, "get_crashguard_settings", return_value=fake_settings), \
         patch("app.crashguard.services.audit.write_audit", new_callable=AsyncMock) as audit_mock, \
         patch("app.crashguard.services.pr_drafter.draft_pr_for_analysis",
               new_callable=AsyncMock, return_value=fake_result):
        await analyzer._maybe_auto_draft_pr(analysis_id=42, feasibility=0.85)

    kwargs = audit_mock.call_args.kwargs
    assert kwargs["success"] is False
    assert kwargs["error"] == "dup_within_30d"
