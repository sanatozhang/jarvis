"""Tests for analysis_worker._effective_version — the repo-routing version source.

Ticket-supplied app_version wins; otherwise fall back to the newest-era version
the extractor resolved from logs (so a native 4.x ticket with no reporter-filled
version still routes to the native band via the log-derived version).
"""
from __future__ import annotations

from app.workers.analysis_worker import _effective_version


class _Issue:
    def __init__(self, app_version=""):
        self.app_version = app_version


def test_ticket_version_wins():
    assert _effective_version(_Issue("4.0.100"), {"app_version": "3.9.0"}) == "4.0.100"


def test_falls_back_to_log_version_when_ticket_empty():
    assert _effective_version(_Issue(""), {"app_version": "4.0.100"}) == "4.0.100"


def test_ticket_whitespace_treated_as_empty():
    assert _effective_version(_Issue("   "), {"app_version": "4.0.100"}) == "4.0.100"


def test_empty_when_neither_present():
    assert _effective_version(_Issue(""), {}) == ""
    assert _effective_version(_Issue(""), None) == ""


def test_no_log_metadata_uses_ticket():
    assert _effective_version(_Issue("3.16.0"), None) == "3.16.0"


def test_missing_attr_safe():
    assert _effective_version(object(), {"app_version": "4.0.100"}) == "4.0.100"
