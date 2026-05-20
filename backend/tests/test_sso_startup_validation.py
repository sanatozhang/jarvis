"""SSO startup fail-fast checks."""
from __future__ import annotations

import pytest

from app.config import SSOSettings
from app.main import _validate_sso_startup


def _ok_sso():
    s = SSOSettings()
    s.enabled = True
    s.google_client_id = "id"
    s.google_client_secret = "secret"
    s.jwt_secret = "x" * 32
    s.google_redirect_uri = "https://apollo.nicebuild.click/api/auth/google/callback"
    return s


def test_passes_with_complete_config():
    _validate_sso_startup(_ok_sso())  # no raise


def test_fails_on_missing_client_id():
    s = _ok_sso(); s.google_client_id = ""
    with pytest.raises(RuntimeError, match="GOOGLE_CLIENT_ID"):
        _validate_sso_startup(s)


def test_fails_on_short_jwt_secret():
    s = _ok_sso(); s.jwt_secret = "short"
    with pytest.raises(RuntimeError, match="SSO_JWT_SECRET"):
        _validate_sso_startup(s)


def test_fails_on_non_https_redirect():
    s = _ok_sso(); s.google_redirect_uri = "http://apollo.local/cb"
    with pytest.raises(RuntimeError, match="https"):
        _validate_sso_startup(s)


def test_disabled_skips_all_checks():
    s = SSOSettings()  # all empty, enabled=False
    _validate_sso_startup(s)  # no raise
