"""SSO startup fail-fast checks."""
from __future__ import annotations

import pytest

from app.config import SSOSettings
from app.main import _validate_sso_startup


def _ok_sso():
    s = SSOSettings()
    s.enabled = True
    s.feishu_app_id = "cli_test"
    s.feishu_app_secret = "secret"
    s.jwt_secret = "x" * 32
    return s


def test_passes_with_complete_config():
    _validate_sso_startup(_ok_sso())


def test_fails_on_missing_app_id():
    s = _ok_sso(); s.feishu_app_id = ""
    with pytest.raises(RuntimeError, match="SSO_FEISHU_APP_ID"):
        _validate_sso_startup(s)


def test_fails_on_missing_app_secret():
    s = _ok_sso(); s.feishu_app_secret = ""
    with pytest.raises(RuntimeError, match="SSO_FEISHU_APP_SECRET"):
        _validate_sso_startup(s)


def test_fails_on_short_jwt_secret():
    s = _ok_sso(); s.jwt_secret = "short"
    with pytest.raises(RuntimeError, match="SSO_JWT_SECRET"):
        _validate_sso_startup(s)


def test_disabled_skips_all_checks():
    s = SSOSettings()
    _validate_sso_startup(s)
