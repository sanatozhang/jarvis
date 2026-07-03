# backend/tests/crashguard/test_symbol_profile.py
"""
TDD Step 1: failing tests for _profile_strategy and symbol_profile routing.

These tests verify the _SYMBOL_PROFILES strategy table and helper function
before implementation is in place.
"""
from app.crashguard.services import symbolication


def test_native_android_skips_dart_symbols():
    # native_android profile：不应调 dart 符号（Flutter 专属）
    prof = symbolication._profile_strategy("native_android")
    assert prof["use_dart_symbols"] is False
    assert prof["use_proguard"] is True


def test_native_ios_skips_flutter_dsym():
    prof = symbolication._profile_strategy("native_ios")
    assert prof["use_flutter_dsym"] is False
    assert prof["use_app_dsym"] is True


def test_flutter_android_uses_dart():
    prof = symbolication._profile_strategy("flutter_android")
    assert prof["use_dart_symbols"] is True


def test_flutter_ios_uses_flutter_dsym():
    prof = symbolication._profile_strategy("flutter_ios")
    assert prof["use_flutter_dsym"] is True
    assert prof["use_dart_symbols"] is True
    assert prof["use_app_dsym"] is False


def test_none_profile_disables_all():
    prof = symbolication._profile_strategy("none")
    assert prof["use_dart_symbols"] is False
    assert prof["use_proguard"] is False
    assert prof["use_native_so"] is False
    assert prof["use_flutter_dsym"] is False
    assert prof["use_app_dsym"] is False


def test_empty_profile_falls_back_gracefully():
    """empty symbol_profile must not blow up — returns a valid dict."""
    prof = symbolication._profile_strategy("")
    assert isinstance(prof, dict)
    assert "use_dart_symbols" in prof


def test_unknown_profile_returns_none_strategy():
    """Unknown profile returns the 'none' strategy (all False)."""
    prof = symbolication._profile_strategy("totally_unknown_profile")
    assert prof["use_dart_symbols"] is False
    assert prof["use_proguard"] is False
