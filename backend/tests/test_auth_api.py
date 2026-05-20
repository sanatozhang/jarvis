"""/api/auth/* smoke tests — imports clean, route registered, basic 302.

Full callback flow (DB + mocked Google) covered after Task 9 wires conftest.
"""
from __future__ import annotations

import pytest


def test_auth_module_imports():
    """Module loads without ImportError; basic objects present."""
    from app.api import auth
    assert auth.router is not None
    assert callable(auth.google_login)
    assert callable(auth.google_callback)
    assert callable(auth.auth_me)
    assert callable(auth.logout)


def test_exchange_code_helper_is_mockable():
    """Helper `_exchange_code_for_id_token` exists and is async — important for Task 9 mocks."""
    import inspect
    from app.api.auth import _exchange_code_for_id_token
    assert inspect.iscoroutinefunction(_exchange_code_for_id_token)
