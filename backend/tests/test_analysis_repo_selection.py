"""Tests for analysis-specific repo selection helpers.

TDD: these tests are written BEFORE the implementation to drive:
  - _os_name_from_issue helper in analysis_worker
  - integration of analysis_path + app-family coexistence fallback
"""
import types
import pytest
from app.services import repo_router as rr

# Reuse the same routing fixture as test_repo_router.py
ROUTING = {
    "android": {"bands": [
        {"min_version": "0", "family": "flutter", "wrapper": "/repos/plaud_ai",
         "sub": "plaud-android", "github_repo": "Plaud-AI/Plaud-App", "symbol_profile": "flutter_android"},
        {"min_version": "4.0.0", "family": "native", "wrapper": "/repos/plaud-native-app",
         "sub": "plaud-native-android", "github_repo": "Plaud-AI/plaud-native-android", "symbol_profile": "native_android"},
    ]},
    "ios": {"bands": [
        {"min_version": "0", "family": "flutter", "wrapper": "/repos/plaud_ai",
         "sub": "plaud-ios", "github_repo": "Plaud-AI/Plaud-App", "symbol_profile": "flutter_ios"},
        {"min_version": "4.0.0", "family": "native", "wrapper": "/repos/plaud-native-app",
         "sub": "plaud-native-ios", "github_repo": "Plaud-AI/plaud-native-ios", "symbol_profile": "native_ios"},
    ]},
    "web": {"bands": [
        {"min_version": "0", "family": "web", "wrapper": "/repos/plaud-web",
         "sub": "", "github_repo": "Plaud-AI/plaud-web", "symbol_profile": "none"},
    ]},
}

ALWAYS = lambda p: True


# ---------------------------------------------------------------------------
# _os_name_from_issue tests
# ---------------------------------------------------------------------------

def test_os_name_from_issue_with_log_metadata_json():
    """Issue with log_metadata_json JSON string extracts os_version field."""
    from app.workers.analysis_worker import _os_name_from_issue
    issue = types.SimpleNamespace(log_metadata_json='{"os_version": "Android 14"}')
    assert _os_name_from_issue(issue) == "Android 14"


def test_os_name_from_issue_malformed_json():
    """Malformed JSON log_metadata_json returns empty string."""
    from app.workers.analysis_worker import _os_name_from_issue
    issue = types.SimpleNamespace(log_metadata_json="not json {{{")
    assert _os_name_from_issue(issue) == ""


def test_os_name_from_issue_missing_attr():
    """Issue without log_metadata_json attribute returns empty string."""
    from app.workers.analysis_worker import _os_name_from_issue
    issue = types.SimpleNamespace()  # no log_metadata_json
    assert _os_name_from_issue(issue) == ""


def test_os_name_from_issue_empty_json():
    """Empty JSON object returns empty string (no os field)."""
    from app.workers.analysis_worker import _os_name_from_issue
    issue = types.SimpleNamespace(log_metadata_json='{}')
    assert _os_name_from_issue(issue) == ""


def test_os_name_from_issue_os_field_fallback():
    """Falls back to 'os' key if 'os_version' missing."""
    from app.workers.analysis_worker import _os_name_from_issue
    issue = types.SimpleNamespace(log_metadata_json='{"os": "iOS 17"}')
    assert _os_name_from_issue(issue) == "iOS 17"


def test_os_name_from_issue_ios():
    """iOS os_version is correctly extracted."""
    from app.workers.analysis_worker import _os_name_from_issue
    issue = types.SimpleNamespace(log_metadata_json='{"os_version": "iOS 17.2"}')
    assert _os_name_from_issue(issue) == "iOS 17.2"


# ---------------------------------------------------------------------------
# analysis_path + disambiguation integration
# ---------------------------------------------------------------------------

def test_resolve_flutter_android_analysis_path_is_wrapper():
    """Flutter android (3.x): analysis_path returns monorepo wrapper, not thin shell."""
    res = rr.resolve("app", "3.5.0", ROUTING, os_name="Android 14", path_exists=ALWAYS)
    assert res is not None and res.family == "flutter"
    assert rr.analysis_path(res) == "/repos/plaud_ai"  # NOT /repos/plaud_ai/plaud-android


def test_resolve_native_ios_analysis_path_is_sub_repo():
    """Native iOS (4.0.0+): analysis_path returns the native sub-repo path."""
    res = rr.resolve("app", "4.0.0", ROUTING, os_name="iOS 17", path_exists=ALWAYS)
    assert res is not None and res.family == "native"
    assert rr.analysis_path(res) == "/repos/plaud-native-app/plaud-native-ios"


def test_resolve_ambiguous_app_no_os_returns_none():
    """platform='app' with no os_name cannot be disambiguated → resolve returns None."""
    res = rr.resolve("app", "3.5.0", ROUTING, os_name="", path_exists=ALWAYS)
    assert res is None
    # analysis_path(None) must be None
    assert rr.analysis_path(res) is None


def test_app_family_coexistence_fallback_pattern():
    """Verify that for an ambiguous 'app' platform, the caller fallback pattern works.

    This test simulates the coexistence fallback logic in analysis_worker:
    If resolve returns None for 'app'/'flutter'/'', the caller should check
    analysis_path on a fallback resolve of 'app' with no os_name disambiguation
    — but since that also returns None, the actual fallback to get_code_repo_for_platform
    is needed. This test just confirms the None path and that analysis_path handles it.
    """
    res = rr.resolve("", "3.0.0", ROUTING, path_exists=ALWAYS)
    assert res is None
    assert rr.analysis_path(None) is None
