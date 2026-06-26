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
