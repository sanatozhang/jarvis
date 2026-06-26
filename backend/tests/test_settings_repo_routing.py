"""Tests for /api/settings/repo-routing endpoints."""
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app


@pytest.mark.asyncio
async def test_preview_resolves(monkeypatch):
    from app.api import settings as st
    monkeypatch.setattr(st, "get_repo_routing", lambda: {"android": {"bands": [
        {"min_version": "4.0.0", "family": "native", "wrapper": "/tmp", "sub": "",
         "github_repo": "Plaud-AI/plaud-native-android", "symbol_profile": "native_android"}]}})
    from app.services import repo_router as rr
    monkeypatch.setattr(rr.os.path, "exists", lambda p: True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/settings/repo-routing/preview", json={"platform": "android", "version": "4.2.0"})
    assert r.status_code == 200
    assert r.json()["family"] == "native"


# ---------------------------------------------------------------------------
# Unit tests for _apply_repo_routing (no DB required)
# ---------------------------------------------------------------------------

class TestApplyRepoRouting:
    """Tests for the _apply_repo_routing() synchronous helper.

    State-cleanup approach: save the original values of Settings.repo_routing
    and CrashguardSettings.datadog_service_filter before each test and restore
    them afterwards. Also call get_settings.cache_clear() + get_crashguard_settings.cache_clear()
    at teardown so mutations don't bleed into other test modules.
    """

    def setup_method(self):
        from app.config import get_settings
        from app.crashguard.config import get_crashguard_settings

        # Ensure we're working with the cached singletons
        self._settings = get_settings()
        self._cg_settings = get_crashguard_settings()

        # Save original values
        self._orig_routing = dict(self._settings.repo_routing)
        self._orig_service_filter = self._cg_settings.datadog_service_filter

    def teardown_method(self):
        from app.config import get_settings
        from app.crashguard.config import get_crashguard_settings

        # Restore originals
        self._settings.repo_routing = self._orig_routing
        self._cg_settings.datadog_service_filter = self._orig_service_filter

        # Clear caches so no mutations leak
        get_settings.cache_clear()
        get_crashguard_settings.cache_clear()

    def test_apply_sets_routing_and_service_filter(self):
        """Providing both 'routing' and 'service_filter' updates both in memory."""
        from app.api.settings import _apply_repo_routing
        from app.config import get_settings
        from app.crashguard.config import get_crashguard_settings

        new_routing = {"android": {"bands": [{"min_version": "0", "family": "flutter",
                                               "wrapper": "/x", "sub": "sub",
                                               "github_repo": "Org/Repo", "symbol_profile": "p"}]}}
        _apply_repo_routing({"routing": new_routing, "service_filter": "service:test-filter"})

        assert get_settings().repo_routing == new_routing
        assert get_crashguard_settings().datadog_service_filter == "service:test-filter"

    def test_apply_routing_only_does_not_touch_service_filter(self):
        """When 'service_filter' key is absent, the existing service_filter is unchanged."""
        from app.api.settings import _apply_repo_routing
        from app.config import get_settings
        from app.crashguard.config import get_crashguard_settings

        original_filter = get_crashguard_settings().datadog_service_filter

        _apply_repo_routing({"routing": {}})

        assert get_settings().repo_routing == {}
        # service_filter unchanged because the key was not present in override
        assert get_crashguard_settings().datadog_service_filter == original_filter

    def test_apply_empty_routing_clears_routing(self):
        """An explicit empty-dict routing replaces whatever was there before."""
        from app.api.settings import _apply_repo_routing
        from app.config import get_settings

        # First set something non-empty
        get_settings().repo_routing = {"android": {"bands": []}}

        _apply_repo_routing({"routing": {}})

        assert get_settings().repo_routing == {}

    def test_apply_service_filter_only_does_not_touch_routing(self):
        """When 'routing' key is absent, existing repo_routing is unchanged."""
        from app.api.settings import _apply_repo_routing
        from app.config import get_settings
        from app.crashguard.config import get_crashguard_settings

        get_settings().repo_routing = {"web": {"bands": []}}

        _apply_repo_routing({"service_filter": "service:only-filter"})

        # routing unchanged
        assert get_settings().repo_routing == {"web": {"bands": []}}
        assert get_crashguard_settings().datadog_service_filter == "service:only-filter"


# ---------------------------------------------------------------------------
# Unit tests for _validate_routing (Fix 3) — pure function, no async needed
# ---------------------------------------------------------------------------

class TestValidateRouting:
    """Tests for _validate_routing() helper added to app/api/settings.py."""

    def test_valid_band_produces_no_warnings(self, tmp_path):
        """A band whose wrapper exists, has .git, and sub exists → no warnings."""
        from app.api.settings import _validate_routing

        wrapper = tmp_path / "native-app"
        wrapper.mkdir()
        (wrapper / ".git").mkdir()
        (wrapper / "plaud-native-android").mkdir()

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
        warnings = _validate_routing(routing)
        assert warnings == []

    def test_nonexistent_wrapper_flagged(self, tmp_path):
        """A band pointing at a wrapper that doesn't exist → warning about wrapper not found."""
        from app.api.settings import _validate_routing

        bad_path = str(tmp_path / "does-not-exist")
        routing = {
            "android": {
                "bands": [
                    {
                        "min_version": "4.0.0",
                        "family": "native",
                        "wrapper": bad_path,
                        "sub": "plaud-native-android",
                    }
                ]
            }
        }
        warnings = _validate_routing(routing)
        assert len(warnings) == 1
        assert warnings[0]["platform"] == "android"
        assert warnings[0]["band"] == 0
        assert "wrapper not found" in warnings[0]["issue"]

    def test_wrapper_without_git_flagged(self, tmp_path):
        """Wrapper exists on disk but lacks .git → warning about not a git repo."""
        from app.api.settings import _validate_routing

        wrapper = tmp_path / "not-a-git-repo"
        wrapper.mkdir()
        # deliberately do NOT create .git

        routing = {
            "ios": {
                "bands": [
                    {
                        "min_version": "0",
                        "family": "flutter",
                        "wrapper": str(wrapper),
                        "sub": "",
                    }
                ]
            }
        }
        warnings = _validate_routing(routing)
        assert len(warnings) == 1
        assert "no .git" in warnings[0]["issue"]

    def test_missing_sub_flagged(self, tmp_path):
        """Wrapper exists with .git but sub directory is absent → warning about sub not found."""
        from app.api.settings import _validate_routing

        wrapper = tmp_path / "native-app"
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
                    }
                ]
            }
        }
        warnings = _validate_routing(routing)
        assert len(warnings) == 1
        assert "sub not found" in warnings[0]["issue"]

    def test_mixed_good_and_bad_band(self, tmp_path):
        """One valid band and one invalid band → exactly one warning for the bad one."""
        from app.api.settings import _validate_routing

        # Good band
        good_wrapper = tmp_path / "good"
        good_wrapper.mkdir()
        (good_wrapper / ".git").mkdir()
        (good_wrapper / "sub-good").mkdir()

        # Bad band (wrapper doesn't exist)
        bad_wrapper = str(tmp_path / "nonexistent")

        routing = {
            "android": {
                "bands": [
                    {
                        "min_version": "0",
                        "family": "flutter",
                        "wrapper": str(good_wrapper),
                        "sub": "sub-good",
                    },
                    {
                        "min_version": "4.0.0",
                        "family": "native",
                        "wrapper": bad_wrapper,
                        "sub": "plaud-native-android",
                    },
                ]
            }
        }
        warnings = _validate_routing(routing)
        # Only the second band (index 1) should trigger a warning
        assert len(warnings) == 1
        assert warnings[0]["band"] == 1
        assert "wrapper not found" in warnings[0]["issue"]

    def test_empty_routing_produces_no_warnings(self):
        """Empty routing dict → no warnings (no bands to check)."""
        from app.api.settings import _validate_routing
        assert _validate_routing({}) == []

    def test_band_without_wrapper_skipped(self):
        """A band with no wrapper key → silently skipped (nothing to validate)."""
        from app.api.settings import _validate_routing
        routing = {
            "android": {
                "bands": [
                    {"min_version": "0", "family": "flutter", "sub": ""}
                ]
            }
        }
        warnings = _validate_routing(routing)
        assert warnings == []
