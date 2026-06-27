import pytest
from app.services import repo_router as rr

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

# 测试里所有路径都"存在"
ALWAYS = lambda p: True


def test_parse_version_strips_build_suffix():
    assert rr.parse_version("3.16.0-634") == (3, 16, 0)
    assert rr.parse_version("4.0.0") == (4, 0, 0)
    assert rr.parse_version("4.2") == (4, 2, 0)
    assert rr.parse_version("") is None
    assert rr.parse_version(None) is None
    assert rr.parse_version("garbage") is None


def test_cutover_boundary_4_0_0():
    # 3.99.0 → flutter；4.0.0 → native（边界归 native）
    r3 = rr.resolve("android", "3.99.0", ROUTING, path_exists=ALWAYS)
    assert r3.family == "flutter" and r3.logical_name == "plaud-android"
    r4 = rr.resolve("android", "4.0.0", ROUTING, path_exists=ALWAYS)
    assert r4.family == "native" and r4.logical_name == "plaud-native-android"
    assert r4.github_repo == "Plaud-AI/plaud-native-android"
    assert r4.symbol_profile == "native_android"
    assert r4.sub_repo_path == "/repos/plaud-native-app/plaud-native-android"
    assert r4.confidence == "high"


def test_version_missing_falls_back_to_newest_band_low_confidence():
    r = rr.resolve("ios", None, ROUTING, path_exists=ALWAYS)
    assert r.family == "native"          # 最新 band
    assert r.confidence == "low"


def test_web_single_band_no_subrepo():
    r = rr.resolve("web", "1.2.3", ROUTING, path_exists=ALWAYS)
    assert r.family == "web"
    assert r.sub_repo_path == "/repos/plaud-web"   # sub 为空 → wrapper 即代码根
    assert r.symbol_profile == "none"


def test_unconfigured_platform_returns_none():
    assert rr.resolve("desktop", "1.0.0", ROUTING, path_exists=ALWAYS) is None


def test_missing_path_returns_none():
    # 路径不存在 → None（降级）
    assert rr.resolve("android", "4.0.0", ROUTING, path_exists=lambda p: False) is None


def test_missing_subrepo_path_returns_none():
    # wrapper exists, sub-repo does not → None
    res = rr.resolve("android", "4.0.0", ROUTING,
                     path_exists=lambda p: p == "/repos/plaud-native-app")
    assert res is None


def test_normalize_platform():
    assert rr.normalize_platform("app", os_name="Android") == "android"
    assert rr.normalize_platform("flutter", os_name="iOS") == "ios"
    assert rr.normalize_platform("ANDROID") == "android"
    assert rr.normalize_platform("web") == "web"
    assert rr.normalize_platform("") is None


# ---------------------------------------------------------------------------
# New tests: normalize_platform edge cases
# ---------------------------------------------------------------------------

def test_normalize_platform_app_ios():
    assert rr.normalize_platform("app", os_name="iOS") == "ios"


def test_normalize_platform_app_android():
    assert rr.normalize_platform("app", os_name="Android") == "android"


def test_normalize_platform_app_no_os():
    # "app" without os_name → cannot disambiguate → None
    assert rr.normalize_platform("app") is None


def test_normalize_platform_flutter_ipadOS():
    # iPadOS contains "ipad" → maps to "ios"
    assert rr.normalize_platform("flutter", os_name="iPadOS") == "ios"


# ---------------------------------------------------------------------------
# New test: resolve with os_name disambiguation
# ---------------------------------------------------------------------------

def test_resolve_app_platform_disambiguated_by_os_name():
    """platform='app' + os_name='Android' should resolve to the android native band."""
    r = rr.resolve("app", "4.0.0", ROUTING, os_name="Android", path_exists=ALWAYS)
    assert r is not None
    assert r.platform == "android"
    assert r.family == "native"
    assert r.logical_name == "plaud-native-android"


# ---------------------------------------------------------------------------
# New test: select_band below-floor returns low confidence
# ---------------------------------------------------------------------------

def test_select_band_below_floor_returns_low_confidence():
    """A version below every band's min_version falls back to oldest band with 'low' confidence."""
    bands = [
        {"min_version": "2.0.0", "family": "old_a"},
        {"min_version": "4.0.0", "family": "native"},
    ]
    # "1.0.0" is below both bands' min_version
    result = rr.select_band(bands, "1.0.0")
    assert result is not None
    band, confidence = result
    assert confidence == "low"
    # The oldest (lowest min_version) band should be returned
    assert band["min_version"] == "2.0.0"


# ---------------------------------------------------------------------------
# New tests: analysis_path helper (TDD RED — will fail until helper is added)
# ---------------------------------------------------------------------------

def test_analysis_path_none_input():
    """analysis_path(None) returns None."""
    assert rr.analysis_path(None) is None


def test_analysis_path_flutter_returns_wrapper():
    """flutter family: analysis_path returns wrapper_path (full monorepo), NOT sub_repo_path."""
    res = rr.resolve("android", "3.5.0", ROUTING, path_exists=ALWAYS)
    assert res is not None and res.family == "flutter"
    # sub_repo_path is /repos/plaud_ai/plaud-android (the thin shell)
    assert res.sub_repo_path == "/repos/plaud_ai/plaud-android"
    # analysis_path must return the monorepo wrapper, not the thin native shell
    assert rr.analysis_path(res) == "/repos/plaud_ai"


def test_analysis_path_native_returns_sub_repo_path():
    """native family: analysis_path returns sub_repo_path (the full native app repo)."""
    res = rr.resolve("android", "4.0.0", ROUTING, path_exists=ALWAYS)
    assert res is not None and res.family == "native"
    assert rr.analysis_path(res) == res.sub_repo_path
    assert rr.analysis_path(res) == "/repos/plaud-native-app/plaud-native-android"


def test_analysis_path_web_returns_sub_repo_path_equals_wrapper():
    """web family: sub_repo_path == wrapper_path (sub is empty), analysis_path returns it."""
    res = rr.resolve("web", "1.2.3", ROUTING, path_exists=ALWAYS)
    assert res is not None and res.family == "web"
    # For web, sub is empty so sub_repo_path == wrapper_path
    assert res.sub_repo_path == res.wrapper_path
    assert rr.analysis_path(res) == res.sub_repo_path
