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


def test_normalize_platform():
    assert rr.normalize_platform("app", os_name="Android") == "android"
    assert rr.normalize_platform("flutter", os_name="iOS") == "ios"
    assert rr.normalize_platform("ANDROID") == "android"
    assert rr.normalize_platform("web") == "web"
    assert rr.normalize_platform("") is None
