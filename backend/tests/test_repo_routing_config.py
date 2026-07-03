# backend/tests/test_repo_routing_config.py
from app import config

def test_get_repo_routing_has_android_ios_bands():
    routing = config.get_repo_routing()
    assert "android" in routing and "ios" in routing
    a = {b["family"] for b in routing["android"]["bands"]}
    assert {"flutter", "native"} <= a

def test_native_band_cutover_is_4():
    routing = config.get_repo_routing()
    native = [b for b in routing["android"]["bands"] if b["family"] == "native"][0]
    assert native["min_version"] == "4.0.0"
    assert native["github_repo"] == "Plaud-AI/plaud-native-android"
    assert native["symbol_profile"] == "native_android"

def test_backfill_from_legacy_env(monkeypatch):
    """Verify legacy env backfill when repo_routing is empty but code_repo_app is set."""
    config.get_settings.cache_clear()
    s = config.get_settings()
    monkeypatch.setattr(s, "repo_routing", {})
    monkeypatch.setattr(s, "code_repo_app", "/legacy/plaud_ai")
    monkeypatch.setattr(s, "code_repo_web", "")
    monkeypatch.setattr(s, "code_repo_desktop", "")
    routing = config.get_repo_routing()
    assert "android" in routing and "ios" in routing
    a = routing["android"]["bands"][0]
    assert a["family"] == "flutter" and a["min_version"] == "0"
    assert a["wrapper"] == "/legacy/plaud_ai" and a["sub"] == "plaud-android"
    config.get_settings.cache_clear()


def test_get_all_code_repos_deduplicates_wrappers(monkeypatch):
    """get_all_code_repos() must return DISTINCT wrapper paths.

    android + ios both have a flutter band pointing at /repos/plaud_ai
    and a native band pointing at /repos/plaud-native-app.
    The result set must contain exactly those 2 distinct wrappers (not 4).
    """
    _routing = {
        "android": {"bands": [
            {"min_version": "0", "family": "flutter",
             "wrapper": "/repos/plaud_ai", "sub": "plaud-android",
             "github_repo": "Plaud-AI/Plaud-App", "symbol_profile": "flutter_android"},
            {"min_version": "4.0.0", "family": "native",
             "wrapper": "/repos/plaud-native-app", "sub": "plaud-native-android",
             "github_repo": "Plaud-AI/plaud-native-android", "symbol_profile": "native_android"},
        ]},
        "ios": {"bands": [
            {"min_version": "0", "family": "flutter",
             "wrapper": "/repos/plaud_ai", "sub": "plaud-ios",
             "github_repo": "Plaud-AI/Plaud-App", "symbol_profile": "flutter_ios"},
            {"min_version": "4.0.0", "family": "native",
             "wrapper": "/repos/plaud-native-app", "sub": "plaud-native-ios",
             "github_repo": "Plaud-AI/plaud-native-ios", "symbol_profile": "native_ios"},
        ]},
    }

    config.get_settings.cache_clear()
    s = config.get_settings()
    monkeypatch.setattr(s, "repo_routing", _routing)
    # Ensure legacy code_repo_* fields don't inject extra entries
    monkeypatch.setattr(s, "code_repo_app", "")
    monkeypatch.setattr(s, "code_repo_path", "")
    monkeypatch.setattr(s, "code_repo_web", "")
    monkeypatch.setattr(s, "code_repo_desktop", "")

    repos = config.get_all_code_repos()

    # Should have exactly 2 distinct wrappers
    assert set(repos.keys()) == {"/repos/plaud_ai", "/repos/plaud-native-app"}
    # No duplicates: number of keys == number of distinct wrappers
    assert len(repos) == 2

    config.get_settings.cache_clear()
