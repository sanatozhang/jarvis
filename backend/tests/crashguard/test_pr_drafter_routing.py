"""Task 5: repo_router-aware PR repo selection + flutter family gate.

Tests:
  1. _resolve_repo_for_issue returns native band for v4.1.0
  2. _should_run_flutter_subrepo_detection gates on family
"""
from __future__ import annotations

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
# New tests: _sample_version
# ---------------------------------------------------------------------------

def test_sample_version_reads_from_representative_stack():
    """Issue with representative_stack JSON → returns sample_app_version."""
    import types
    from app.crashguard.services import pr_drafter

    issue = types.SimpleNamespace(
        representative_stack='{"sample_app_version": "4.1.0-720"}',
        app_version="3.0.0",
    )
    assert pr_drafter._sample_version(issue) == "4.1.0-720"


def test_sample_version_falls_back_on_malformed_json():
    """Malformed JSON in representative_stack → falls back to app_version."""
    import types
    from app.crashguard.services import pr_drafter

    issue = types.SimpleNamespace(
        representative_stack="{not valid json!!!",
        app_version="3.16.0-634",
    )
    assert pr_drafter._sample_version(issue) == "3.16.0-634"


def test_sample_version_falls_back_when_field_absent():
    """representative_stack JSON present but sample_app_version key absent → app_version."""
    import types
    from app.crashguard.services import pr_drafter

    issue = types.SimpleNamespace(
        representative_stack='{"other_field": "x"}',
        app_version="2.5.0",
    )
    assert pr_drafter._sample_version(issue) == "2.5.0"


def test_sample_version_empty_when_neither_field():
    """Neither representative_stack nor app_version → empty string."""
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
