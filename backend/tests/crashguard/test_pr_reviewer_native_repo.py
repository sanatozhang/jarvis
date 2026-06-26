"""Tests for _resolve_repo_path_for_pr with native (non-flutter) repo names.

Fix 2: before the final `return ""`, pr_reviewer now looks up the repo name in
get_repo_routing() to support plaud-native-android / plaud-native-ios and any
future repos introduced via repo_routing config.
"""
from __future__ import annotations

import os
import types

import pytest


def _make_settings(**kwargs):
    """Minimal settings stub with common repo path attrs."""
    defaults = {
        "repo_path_flutter": "",
        "repo_path_android": "",
        "repo_path_ios": "",
    }
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


def _make_pr(repo: str, pr_url: str = "https://github.com/Plaud-AI/plaud-native-android/pull/1"):
    return types.SimpleNamespace(repo=repo, pr_url=pr_url)


# ---------------------------------------------------------------------------
# Fix 2: native repo resolved via routing config
# ---------------------------------------------------------------------------

def test_resolve_native_android_via_routing(tmp_path, monkeypatch):
    """pr.repo='plaud-native-android' → resolved via repo_routing lookup."""
    # Set up a real directory tree that the lookup will validate
    wrapper = tmp_path / "plaud-native-app"
    wrapper.mkdir()
    (wrapper / ".git").mkdir()
    sub_dir = wrapper / "plaud-native-android"
    sub_dir.mkdir()

    routing = {
        "android": {
            "bands": [
                {
                    "min_version": "4.0.0",
                    "family": "native",
                    "wrapper": str(wrapper),
                    "sub": "plaud-native-android",
                    "github_repo": "Plaud-AI/plaud-native-android",
                    "symbol_profile": "native_android",
                }
            ]
        }
    }

    import app.api.settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_repo_routing", lambda: routing)

    # Also patch get_repo_routing inside pr_reviewer (it imports from app.config)
    import app.config as config_mod
    monkeypatch.setattr(config_mod, "get_repo_routing", lambda: routing)

    from app.crashguard.services.pr_reviewer import _resolve_repo_path_for_pr

    pr = _make_pr("plaud-native-android")
    settings = _make_settings()
    result = _resolve_repo_path_for_pr(pr, settings)

    expected = os.path.join(str(wrapper), "plaud-native-android")
    assert result == expected, f"Expected {expected!r}, got {result!r}"


def test_resolve_native_ios_via_routing(tmp_path, monkeypatch):
    """pr.repo='plaud-native-ios' → resolved via repo_routing lookup."""
    wrapper = tmp_path / "plaud-native-app"
    wrapper.mkdir()
    (wrapper / ".git").mkdir()
    sub_dir = wrapper / "plaud-native-ios"
    sub_dir.mkdir()

    routing = {
        "ios": {
            "bands": [
                {
                    "min_version": "4.0.0",
                    "family": "native",
                    "wrapper": str(wrapper),
                    "sub": "plaud-native-ios",
                    "github_repo": "Plaud-AI/plaud-native-ios",
                    "symbol_profile": "native_ios",
                }
            ]
        }
    }

    import app.config as config_mod
    monkeypatch.setattr(config_mod, "get_repo_routing", lambda: routing)

    from app.crashguard.services.pr_reviewer import _resolve_repo_path_for_pr

    pr = _make_pr("plaud-native-ios", pr_url="https://github.com/Plaud-AI/plaud-native-ios/pull/5")
    settings = _make_settings()
    result = _resolve_repo_path_for_pr(pr, settings)

    expected = os.path.join(str(wrapper), "plaud-native-ios")
    assert result == expected


def test_resolve_unknown_repo_returns_empty(tmp_path, monkeypatch):
    """An unrecognised repo name not in routing → returns empty string (no crash)."""
    import app.config as config_mod
    monkeypatch.setattr(config_mod, "get_repo_routing", lambda: {})

    from app.crashguard.services.pr_reviewer import _resolve_repo_path_for_pr

    pr = _make_pr("plaud-unknown-repo")
    settings = _make_settings()
    result = _resolve_repo_path_for_pr(pr, settings)
    assert result == ""


def test_resolve_existing_android_branch_unchanged(monkeypatch):
    """The legacy 'plaud-android' branch (repo_path_android) still works (backward compat)."""
    import app.config as config_mod
    monkeypatch.setattr(config_mod, "get_repo_routing", lambda: {})

    from app.crashguard.services.pr_reviewer import _resolve_repo_path_for_pr

    pr = _make_pr("plaud-android")
    settings = _make_settings(repo_path_android="/legacy/android")
    result = _resolve_repo_path_for_pr(pr, settings)
    assert result == "/legacy/android"


def test_resolve_routing_sub_missing_returns_empty(tmp_path, monkeypatch):
    """Routing entry matches but sub path doesn't exist on disk → returns empty string."""
    wrapper = tmp_path / "plaud-native-app"
    wrapper.mkdir()
    (wrapper / ".git").mkdir()
    # deliberately do NOT create the sub directory

    routing = {
        "android": {
            "bands": [
                {
                    "min_version": "4.0.0",
                    "family": "native",
                    "wrapper": str(wrapper),
                    "sub": "plaud-native-android",
                    "github_repo": "Plaud-AI/plaud-native-android",
                    "symbol_profile": "native_android",
                }
            ]
        }
    }

    import app.config as config_mod
    monkeypatch.setattr(config_mod, "get_repo_routing", lambda: routing)

    from app.crashguard.services.pr_reviewer import _resolve_repo_path_for_pr

    pr = _make_pr("plaud-native-android")
    settings = _make_settings()
    result = _resolve_repo_path_for_pr(pr, settings)
    assert result == ""
