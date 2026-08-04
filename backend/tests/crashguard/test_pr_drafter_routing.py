"""Task 5: repo_router-aware PR repo selection + flutter family gate.

Tests:
  1. _resolve_repo_for_issue returns native band for v4.1.0
  2. _should_run_flutter_subrepo_detection gates on family
"""
from __future__ import annotations

import json

import pytest
from app.services import repo_router as _rr

# Save the original resolve before any monkeypatching
_original_resolve = _rr.resolve

# Shared routing fixture for android with two bands (flutter/native)
_ANDROID_ROUTING = {"android": {"bands": [
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
        "wrapper": "/r/plaud-native-app",
        "sub": "plaud-native-android",
        "github_repo": "Plaud-AI/plaud-native-android",
        "symbol_profile": "native_android",
    },
]}}


def _resolve_with_path_exists_true(p, v, r, **kw):
    """Wrapper around the original resolve() that forces path_exists=True."""
    return _original_resolve(p, v, r, path_exists=lambda _: True)


def test_resolve_native_repo_for_v4(monkeypatch):
    """v4.1.0 on android → native band, not flutter band."""
    from app.crashguard.services import pr_drafter

    # patch get_repo_routing used inside pr_drafter
    monkeypatch.setattr(pr_drafter, "get_repo_routing", lambda: _ANDROID_ROUTING)
    # patch repo_router.resolve so path_exists=True (test paths /r/... don't exist)
    monkeypatch.setattr(pr_drafter.repo_router, "resolve", _resolve_with_path_exists_true)

    res = pr_drafter._resolve_repo_for_issue("android", "4.1.0-720")
    assert res is not None, "Expected a RepoResolution, got None"
    assert res.family == "native"
    assert res.github_repo == "Plaud-AI/plaud-native-android"
    assert res.sub_repo_path.endswith("plaud-native-android")


def test_resolve_flutter_repo_for_v3(monkeypatch):
    """v3.16.0 on android → flutter band."""
    from app.crashguard.services import pr_drafter

    monkeypatch.setattr(pr_drafter, "get_repo_routing", lambda: _ANDROID_ROUTING)
    monkeypatch.setattr(pr_drafter.repo_router, "resolve", _resolve_with_path_exists_true)

    res = pr_drafter._resolve_repo_for_issue("android", "3.16.0-634")
    assert res is not None
    assert res.family == "flutter"
    assert res.github_repo == "Plaud-AI/Plaud-App"


def test_resolve_returns_none_for_unknown_platform(monkeypatch):
    """Unknown platform returns None (no crash)."""
    from app.crashguard.services import pr_drafter

    monkeypatch.setattr(pr_drafter, "get_repo_routing", lambda: {})
    monkeypatch.setattr(pr_drafter.repo_router, "resolve", _resolve_with_path_exists_true)

    res = pr_drafter._resolve_repo_for_issue("unknown_platform", "1.0.0")
    assert res is None


def test_flutter_subrepo_detection_gated_to_flutter():
    """native/desktop family must NOT trigger global/cn blob detection."""
    from app.crashguard.services import pr_drafter

    assert pr_drafter._should_run_flutter_subrepo_detection("native") is False
    assert pr_drafter._should_run_flutter_subrepo_detection("flutter") is True
    assert pr_drafter._should_run_flutter_subrepo_detection("desktop") is False
    assert pr_drafter._should_run_flutter_subrepo_detection("") is False
    assert pr_drafter._should_run_flutter_subrepo_detection("FLUTTER") is True


# ---------------------------------------------------------------------------
# New tests: _first_dominant_value
# ---------------------------------------------------------------------------

def test_first_dominant_value_strips_trailing_pct():
    from app.crashguard.services import pr_drafter

    assert pr_drafter._first_dominant_value(
        "4.0.100-970 (40.0%), 4.0.100-963 (20.0%)"
    ) == "4.0.100-970"


def test_first_dominant_value_empty_input():
    from app.crashguard.services import pr_drafter

    assert pr_drafter._first_dominant_value("") == ""


def test_first_dominant_value_single_value_no_pct():
    from app.crashguard.services import pr_drafter

    assert pr_drafter._first_dominant_value("  4.0.100-970  ") == "4.0.100-970"


# ---------------------------------------------------------------------------
# New tests: _sample_version
# ---------------------------------------------------------------------------

def test_sample_version_reads_from_top_app_version():
    """Issue with top_app_version distribution string → returns cleaned first (dominant) version."""
    import types
    from app.crashguard.services import pr_drafter

    issue = types.SimpleNamespace(
        top_app_version="4.0.100-970 (40.0%), 4.0.100-963 (20.0%)",
        stack_variants="[]",
    )
    assert pr_drafter._sample_version(issue) == "4.0.100-970"


def test_sample_version_falls_back_to_stack_variants_is_main():
    """top_app_version empty → falls back to stack_variants entry with is_main=True."""
    import types
    from app.crashguard.services import pr_drafter

    issue = types.SimpleNamespace(
        top_app_version="",
        stack_variants=json.dumps([
            {"sample_app_version": "3.19.0-717", "is_main": False},
            {"sample_app_version": "3.20.0-800", "is_main": True},
        ]),
    )
    assert pr_drafter._sample_version(issue) == "3.20.0-800"


def test_sample_version_falls_back_to_first_variant_when_no_is_main():
    """top_app_version empty, no variant has is_main=True → uses first variant."""
    import types
    from app.crashguard.services import pr_drafter

    issue = types.SimpleNamespace(
        top_app_version="",
        stack_variants=json.dumps([
            {"sample_app_version": "3.19.0-717", "is_main": False},
            {"sample_app_version": "3.20.0-800", "is_main": False},
        ]),
    )
    assert pr_drafter._sample_version(issue) == "3.19.0-717"


def test_sample_version_falls_back_to_last_seen_version():
    """最终修复轮问题 5 回归测试：top_app_version 和 stack_variants 都拿不到值时
    （RUM 拿到了 os 分布但 version 分布恰好为空——两个字段各自独立守卫写入），
    必须回退 issue.last_seen_version——这个字段由 Datadog 摄取路径无条件写入，
    不依赖 RUM 分布数据是否拉到，是比空字符串更可靠的最终兜底。
    `repo_router.select_band` 对 version 缺失的处理是"取 min_version 最大的 band"，
    返回 "" 会把老版本崩溃误路由到最新 native band。"""
    import types
    from app.crashguard.services import pr_drafter

    issue = types.SimpleNamespace(
        top_app_version="",
        stack_variants="[]",
        last_seen_version="3.14.2-590",
    )
    assert pr_drafter._sample_version(issue) == "3.14.2-590"


def test_sample_version_empty_when_neither_field():
    """top_app_version / stack_variants / last_seen_version 全都拿不到值 → 空字符串
    （保留现有"version 缺失→confidence=low"的兜底行为）。"""
    import types
    from app.crashguard.services import pr_drafter

    issue = types.SimpleNamespace()  # no attributes at all
    assert pr_drafter._sample_version(issue) == ""


# ---------------------------------------------------------------------------
# New tests: _select_candidates (Fix 1)
# ---------------------------------------------------------------------------

def test_select_candidates_native_with_res_returns_single():
    """Native family + non-None res → single-element list from res (no fallback)."""
    import types
    from app.crashguard.services import pr_drafter

    res = types.SimpleNamespace(logical_name="plaud-native-android", sub_repo_path="/r/plaud-native-app/plaud-native-android")
    fallback_called = []

    def fallback():
        fallback_called.append(True)
        return [("flutter-global", "/r/flutter/global"), ("flutter-cn", "/r/flutter/cn")]

    result = pr_drafter._select_candidates("native", res, fallback)
    assert result == [("plaud-native-android", "/r/plaud-native-app/plaud-native-android")]
    assert not fallback_called, "fallback must NOT be called for native family with valid res"


def test_select_candidates_desktop_with_res_returns_single():
    """Desktop family + non-None res → single-element list (same short-circuit as native)."""
    import types
    from app.crashguard.services import pr_drafter

    res = types.SimpleNamespace(logical_name="plaud-desktop-win", sub_repo_path="/r/desktop/win")
    fallback_called = []

    def fallback():
        fallback_called.append(True)
        return []

    result = pr_drafter._select_candidates("desktop", res, fallback)
    assert result == [("plaud-desktop-win", "/r/desktop/win")]
    assert not fallback_called


def test_select_candidates_flutter_falls_through_to_fallback():
    """Flutter family → always falls through to fallback_callable (blob detection needed)."""
    import types
    from app.crashguard.services import pr_drafter

    res = types.SimpleNamespace(logical_name="plaud-flutter-global", sub_repo_path="/r/plaud_ai/plaud-flutter-global")

    expected = [("flutter-global", "/r/g"), ("flutter-cn", "/r/cn")]
    result = pr_drafter._select_candidates("flutter", res, lambda: expected)
    assert result is expected


def test_select_candidates_native_none_res_falls_through_to_fallback():
    """Native family + None res → resolution failed, fall through to fallback_callable."""
    from app.crashguard.services import pr_drafter

    expected = [("plaud-android", "/r/fallback")]
    result = pr_drafter._select_candidates("native", None, lambda: expected)
    assert result is expected


# ---------------------------------------------------------------------------
# B2: _resolve_repo_for_issue passes os_name through to repo_router.resolve,
# so a literal platform="flutter" can be disambiguated to android/ios.
# ---------------------------------------------------------------------------

def test_resolve_repo_for_issue_forwards_os_name(monkeypatch):
    """_resolve_repo_for_issue(platform, version, os_name=...) must forward os_name
    to repo_router.resolve — this is what lets a literal 'flutter' platform value
    resolve to the android/ios band instead of returning None."""
    from app.crashguard.services import pr_drafter

    captured = {}

    def _capture_resolve(platform, version, routing, **kwargs):
        captured["platform"] = platform
        captured["version"] = version
        captured["os_name"] = kwargs.get("os_name")
        return "sentinel"

    monkeypatch.setattr(pr_drafter.repo_router, "resolve", _capture_resolve)
    result = pr_drafter._resolve_repo_for_issue("flutter", "3.2.0", os_name="Android 14")

    assert result == "sentinel"
    assert captured["platform"] == "flutter"
    assert captured["version"] == "3.2.0"
    assert captured["os_name"] == "Android 14"


def test_resolve_repo_for_issue_os_name_defaults_to_empty(monkeypatch):
    """Default os_name="" must not break existing callers that don't pass it."""
    from app.crashguard.services import pr_drafter

    captured = {}

    def _capture_resolve(platform, version, routing, **kwargs):
        captured["os_name"] = kwargs.get("os_name")
        return None

    monkeypatch.setattr(pr_drafter.repo_router, "resolve", _capture_resolve)
    pr_drafter._resolve_repo_for_issue("android", "4.1.0")

    assert captured["os_name"] == ""


# ---------------------------------------------------------------------------
# B2: the 3 real call sites inside pr_drafter (draft_pr_for_analysis with and
# without repo_override, draft_prs_multi's family-derivation call) must each
# pass os_name derived from issue.top_os through to repo_router.resolve.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_draft_pr_for_analysis_no_override_passes_top_os_as_os_name(tmp_path, monkeypatch):
    """Call site ~1775 (no repo_override branch): os_name must come from issue.top_os."""
    from unittest.mock import AsyncMock

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'b2_no_override.db'}")
    from app.db.database import get_session, init_db
    from app.crashguard import models  # noqa: F401
    from app.crashguard.models import CrashAnalysis, CrashIssue
    from app.crashguard.services import pr_drafter

    await init_db()
    issue_id = "ddi_b2_no_override"
    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id=issue_id,
            platform="flutter",
            title="crash",
            top_app_version="3.2.0",
            top_os="Android 14 (80.0%), Android 13 (20.0%)",
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

    calls = []

    def _capture_resolve(platform, version, routing, **kwargs):
        calls.append(kwargs.get("os_name"))
        return None

    monkeypatch.setattr(pr_drafter, "get_repo_routing", lambda: {})
    monkeypatch.setattr(pr_drafter.repo_router, "resolve", _capture_resolve)
    monkeypatch.setattr(
        pr_drafter, "get_crashguard_settings",
        lambda: type("S", (), {
            "pr_enabled": True,
            "scheduler_enabled": True,
            "pr_dedup_days": 30,
            "repo_path_flutter": "", "repo_path_android": "", "repo_path_ios": "",
        })(),
    )
    monkeypatch.setattr("app.config.get_code_repo_for_platform", lambda *a, **k: "")
    monkeypatch.setattr("app.crashguard.services.audit.write_audit", AsyncMock())

    result = await pr_drafter.draft_pr_for_analysis(analysis_id, approver="human")

    assert result["ok"] is False  # repo_path unresolved → expected early exit
    assert calls, "repo_router.resolve was never called"
    assert calls[0] == "Android 14"


@pytest.mark.asyncio
async def test_draft_pr_for_analysis_with_override_passes_top_os_as_os_name(tmp_path, monkeypatch):
    """Call site ~1769 (repo_override branch): os_name must come from issue.top_os."""
    from unittest.mock import AsyncMock

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'b2_with_override.db'}")
    from app.db.database import get_session, init_db
    from app.crashguard import models  # noqa: F401
    from app.crashguard.models import CrashAnalysis, CrashIssue
    from app.crashguard.services import pr_drafter

    await init_db()
    issue_id = "ddi_b2_with_override"
    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id=issue_id,
            platform="flutter",
            title="crash",
            top_app_version="3.2.0",
            top_os="iOS 17 (90.0%), iOS 16 (10.0%)",
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

    calls = []

    def _capture_resolve(platform, version, routing, **kwargs):
        calls.append(kwargs.get("os_name"))
        return None

    monkeypatch.setattr(pr_drafter, "get_repo_routing", lambda: {})
    monkeypatch.setattr(pr_drafter.repo_router, "resolve", _capture_resolve)
    monkeypatch.setattr(
        pr_drafter, "get_crashguard_settings",
        lambda: type("S", (), {
            "pr_enabled": True,
            "scheduler_enabled": True,
            "pr_dedup_days": 30,
            "repo_path_flutter": "", "repo_path_android": "", "repo_path_ios": "",
        })(),
    )
    monkeypatch.setattr("app.crashguard.services.audit.write_audit", AsyncMock())

    result = await pr_drafter.draft_pr_for_analysis(
        analysis_id, approver="human", repo_override=("plaud-flutter-global", "/nonexistent/fake/path"),
    )

    assert result["ok"] is False  # fake repo path doesn't exist → expected early exit
    assert calls, "repo_router.resolve was never called"
    assert calls[0] == "iOS 17"


@pytest.mark.asyncio
async def test_draft_prs_multi_family_derivation_passes_top_os_as_os_name(tmp_path, monkeypatch):
    """Call site ~2521 (draft_prs_multi family derivation): os_name from issue.top_os."""
    from unittest.mock import AsyncMock

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'b2_family.db'}")
    from app.db.database import get_session, init_db
    from app.crashguard import models  # noqa: F401
    from app.crashguard.models import CrashAnalysis, CrashIssue
    from app.crashguard.services import pr_drafter

    await init_db()
    issue_id = "ddi_b2_family"
    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id=issue_id,
            platform="flutter",
            title="crash",
            top_app_version="3.2.0",
            top_os="Android 14 (100.0%)",
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

    calls = []

    def _capture_resolve(platform, version, routing, **kwargs):
        calls.append(kwargs.get("os_name"))
        return None

    monkeypatch.setattr(pr_drafter, "get_repo_routing", lambda: {})
    monkeypatch.setattr(pr_drafter.repo_router, "resolve", _capture_resolve)
    monkeypatch.setattr(
        pr_drafter, "get_crashguard_settings",
        lambda: type("S", (), {
            "pr_enabled": True,
            "scheduler_enabled": True,
            "pr_dedup_days": 30,
            "gate_primary_only_enabled": True,
            "repo_path_flutter": "", "repo_path_android": "", "repo_path_ios": "",
        })(),
    )
    # Short-circuit candidate selection so we only exercise the family-derivation
    # call to _resolve_repo_for_issue, not the heavier blob-detection machinery.
    monkeypatch.setattr(pr_drafter, "_select_candidates", lambda *a, **k: [])
    monkeypatch.setattr("app.config.get_code_repo_for_platform", lambda *a, **k: "")
    monkeypatch.setattr("app.crashguard.services.audit.write_audit", AsyncMock())

    result = await pr_drafter.draft_prs_multi(analysis_id, approver="human")

    assert result["ok"] is False
    assert calls, "repo_router.resolve was never called"
    # First call is the family-derivation call inside draft_prs_multi itself.
    assert calls[0] == "Android 14"
