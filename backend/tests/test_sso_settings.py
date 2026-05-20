"""SSO settings parsing & defaults."""
from __future__ import annotations

import os
from unittest.mock import patch

from app.config import SSOSettings


def test_sso_disabled_by_default():
    s = SSOSettings()
    assert s.enabled is False
    assert s.cookie_days == 365


def test_sso_parses_allowed_domains_csv(monkeypatch):
    monkeypatch.setenv("SSO_ALLOWED_DOMAINS", "plaud.ai,foo.com")
    s = SSOSettings()
    assert s.allowed_domains == ["plaud.ai", "foo.com"]


def test_sso_parses_admin_emails_csv(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "a@plaud.ai, b@plaud.ai")
    s = SSOSettings()
    assert s.admin_emails == ["a@plaud.ai", "b@plaud.ai"]


def test_sso_parses_exempt_paths_csv(monkeypatch):
    monkeypatch.setenv("SSO_EXEMPT_PATHS", "/api/health,/api/v1/")
    s = SSOSettings()
    assert s.exempt_paths == ["/api/health", "/api/v1/"]
