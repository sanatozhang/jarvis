"""Family-scoped auto-PR switch (2026-07-13): pr_enabled_flutter / pr_enabled_native.

Context: 3.x(flutter) auto-PR paused, 4.0 native(android/ios) auto-PR resumed —
independent of the global `pr_enabled` kill switch. The gate must only apply to
automatic triggers (approver in {auto, auto_retry, top_auto}); a human explicitly
clicking approve (or the backfill sweep) must never be blocked by it.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app.services import repo_router as _rr

_original_resolve = _rr.resolve

_ROUTING = {"android": {"bands": [
    {
        "min_version": "0",
        "family": "flutter",
        "wrapper": "/r/plaud_ai",
        "sub": "plaud-android",
        "github_repo": "Plaud-AI/Plaud-App",
        "symbol_profile": "flutter_android",
    },
    {
        "min_version": "4.0.0",
        "family": "native",
        "wrapper": "/r/plaud_ai",
        "sub": "plaud-native-android",
        "github_repo": "Plaud-AI/plaud-native-android",
        "symbol_profile": "native_android",
    },
]}}


def _resolve_with_path_exists_true(p, v, r, **kw):
    return _original_resolve(p, v, r, path_exists=lambda _: True)


def _fake_settings(*, pr_enabled=True, pr_enabled_flutter=True, pr_enabled_native=True):
    return type("S", (), {
        "pr_enabled": pr_enabled,
        "pr_enabled_flutter": pr_enabled_flutter,
        "pr_enabled_native": pr_enabled_native,
        "pr_dedup_days": 30,
        "gate_primary_only_enabled": True,
        "scheduler_enabled": True,
    })()


async def _seed(monkeypatch, tmp_path, *, sample_version: str, issue_id: str):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / f'{issue_id}.db'}")
    from app.db.database import get_session, init_db
    from app.crashguard import models  # noqa: F401
    from app.crashguard.models import CrashAnalysis, CrashIssue

    await init_db()
    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id=issue_id,
            platform="android",
            title="crash",
            representative_stack=json.dumps({"sample_app_version": sample_version}),
        ))
        ana = CrashAnalysis(
            datadog_issue_id=issue_id,
            analysis_run_id=f"run-{issue_id}",
            status="success",
            followup_question="",
            root_cause="root",
            fix_suggestion="fix lib/foo",
            feasibility_score=0.9,
        )
        session.add(ana)
        await session.commit()
        return ana.id


@pytest.mark.asyncio
async def test_auto_blocked_when_flutter_family_disabled(tmp_path, monkeypatch):
    from app.crashguard.services import pr_drafter

    monkeypatch.setattr(pr_drafter, "get_repo_routing", lambda: _ROUTING)
    monkeypatch.setattr(pr_drafter.repo_router, "resolve", _resolve_with_path_exists_true)
    monkeypatch.setattr(
        pr_drafter, "get_crashguard_settings",
        lambda: _fake_settings(pr_enabled_flutter=False, pr_enabled_native=True),
    )

    analysis_id = await _seed(monkeypatch, tmp_path, sample_version="3.16.0-634", issue_id="ddi_flutter_auto")

    result = await pr_drafter.draft_prs_multi(analysis_id, approver="auto")
    assert result == {"ok": False, "error": "pr_disabled_for_family:flutter", "prs": []}


@pytest.mark.asyncio
async def test_auto_blocked_when_native_family_disabled(tmp_path, monkeypatch):
    from app.crashguard.services import pr_drafter

    monkeypatch.setattr(pr_drafter, "get_repo_routing", lambda: _ROUTING)
    monkeypatch.setattr(pr_drafter.repo_router, "resolve", _resolve_with_path_exists_true)
    monkeypatch.setattr(
        pr_drafter, "get_crashguard_settings",
        lambda: _fake_settings(pr_enabled_flutter=True, pr_enabled_native=False),
    )

    analysis_id = await _seed(monkeypatch, tmp_path, sample_version="4.1.0-720", issue_id="ddi_native_auto")

    result = await pr_drafter.draft_prs_multi(analysis_id, approver="top_auto")
    assert result == {"ok": False, "error": "pr_disabled_for_family:native", "prs": []}


@pytest.mark.asyncio
async def test_auto_not_blocked_when_native_family_enabled(tmp_path, monkeypatch):
    """native + pr_enabled_native=True → gate passes through to candidate resolution."""
    from app.crashguard.services import pr_drafter

    monkeypatch.setattr(pr_drafter, "get_repo_routing", lambda: _ROUTING)
    monkeypatch.setattr(pr_drafter.repo_router, "resolve", _resolve_with_path_exists_true)
    monkeypatch.setattr(
        pr_drafter, "get_crashguard_settings",
        lambda: _fake_settings(pr_enabled_flutter=False, pr_enabled_native=True),
    )
    monkeypatch.setattr(pr_drafter, "_select_candidates", lambda *a, **k: [("plaud-native-android", "/fake/path")])
    stub = AsyncMock(return_value={"ok": True, "pr_url": "https://github.com/o/r/pull/1", "repo": "plaud-native-android"})
    monkeypatch.setattr(pr_drafter, "draft_pr_for_analysis", stub)

    analysis_id = await _seed(monkeypatch, tmp_path, sample_version="4.1.0-720", issue_id="ddi_native_auto_ok")

    result = await pr_drafter.draft_prs_multi(analysis_id, approver="auto")
    stub.assert_awaited_once()
    assert result["succeeded"] == 1


@pytest.mark.asyncio
async def test_human_approver_bypasses_family_gate(tmp_path, monkeypatch):
    """Human explicitly approving a flutter-family PR must not be blocked, even
    while pr_enabled_flutter=False pauses the automatic pipeline."""
    from app.crashguard.services import pr_drafter

    monkeypatch.setattr(pr_drafter, "get_repo_routing", lambda: _ROUTING)
    monkeypatch.setattr(pr_drafter.repo_router, "resolve", _resolve_with_path_exists_true)
    monkeypatch.setattr(
        pr_drafter, "get_crashguard_settings",
        lambda: _fake_settings(pr_enabled_flutter=False, pr_enabled_native=True),
    )
    monkeypatch.setattr(pr_drafter, "_select_candidates", lambda *a, **k: [("plaud-android", "/fake/path")])
    stub = AsyncMock(return_value={"ok": True, "pr_url": "https://github.com/o/r/pull/2", "repo": "plaud-android"})
    monkeypatch.setattr(pr_drafter, "draft_pr_for_analysis", stub)

    analysis_id = await _seed(monkeypatch, tmp_path, sample_version="3.16.0-634", issue_id="ddi_flutter_human")

    result = await pr_drafter.draft_prs_multi(analysis_id, approver="human")
    stub.assert_awaited_once()
    assert result["succeeded"] == 1


@pytest.mark.asyncio
async def test_backfill_approver_bypasses_family_gate(tmp_path, monkeypatch):
    from app.crashguard.services import pr_drafter

    monkeypatch.setattr(pr_drafter, "get_repo_routing", lambda: _ROUTING)
    monkeypatch.setattr(pr_drafter.repo_router, "resolve", _resolve_with_path_exists_true)
    monkeypatch.setattr(
        pr_drafter, "get_crashguard_settings",
        lambda: _fake_settings(pr_enabled_flutter=False, pr_enabled_native=True),
    )
    monkeypatch.setattr(pr_drafter, "_select_candidates", lambda *a, **k: [("plaud-android", "/fake/path")])
    stub = AsyncMock(return_value={"ok": True, "pr_url": "https://github.com/o/r/pull/3", "repo": "plaud-android"})
    monkeypatch.setattr(pr_drafter, "draft_pr_for_analysis", stub)

    analysis_id = await _seed(monkeypatch, tmp_path, sample_version="3.16.0-634", issue_id="ddi_flutter_backfill")

    result = await pr_drafter.draft_prs_multi(analysis_id, approver="backfill")
    stub.assert_awaited_once()
    assert result["succeeded"] == 1
