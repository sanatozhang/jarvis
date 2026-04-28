"""三维分类器测试"""
from __future__ import annotations

import pytest


def test_is_new_in_version_true_when_first_seen_matches_latest():
    """issue 的 first_seen_version 等于当前最新发布版 → is_new_in_version=True"""
    from app.crashguard.services.classifier import is_new_in_version

    assert is_new_in_version(
        first_seen_version="1.4.7",
        latest_release="1.4.7",
    ) is True


def test_is_new_in_version_false_for_old_issue():
    """老 issue（first_seen_version 早于最新版）→ False"""
    from app.crashguard.services.classifier import is_new_in_version

    assert is_new_in_version(
        first_seen_version="1.4.5",
        latest_release="1.4.7",
    ) is False


def test_is_new_in_version_handles_missing():
    """缺数据时返回 False（保守）"""
    from app.crashguard.services.classifier import is_new_in_version

    assert is_new_in_version(first_seen_version="", latest_release="1.4.7") is False
    assert is_new_in_version(first_seen_version="1.4.7", latest_release="") is False


def test_is_regression_when_silent_then_returns():
    """fingerprint 在 v1.4.4 出现，1.4.5/1.4.6/1.4.7 都静默，今日 v1.4.8 又出现 → True"""
    from app.crashguard.services.classifier import is_regression

    assert is_regression(
        fingerprint_seen_versions=["1.4.4"],
        recent_versions=["1.4.5", "1.4.6", "1.4.7"],
        current_version="1.4.8",
        silent_threshold=3,
    ) is True


def test_is_regression_false_when_continuously_present():
    """连续出现，从未静默 → False"""
    from app.crashguard.services.classifier import is_regression

    assert is_regression(
        fingerprint_seen_versions=["1.4.4", "1.4.5", "1.4.6", "1.4.7"],
        recent_versions=["1.4.5", "1.4.6", "1.4.7"],
        current_version="1.4.8",
        silent_threshold=3,
    ) is False


def test_is_regression_false_for_brand_new_fingerprint():
    """全新 fingerprint（之前从未出现）→ 不算 regression（应归为 is_new_in_version）"""
    from app.crashguard.services.classifier import is_regression

    assert is_regression(
        fingerprint_seen_versions=[],
        recent_versions=["1.4.5", "1.4.6", "1.4.7"],
        current_version="1.4.8",
        silent_threshold=3,
    ) is False


def test_is_regression_false_when_silence_too_short():
    """只静默 1 个版本（少于 threshold=3）→ False"""
    from app.crashguard.services.classifier import is_regression

    assert is_regression(
        fingerprint_seen_versions=["1.4.6"],
        recent_versions=["1.4.7"],
        current_version="1.4.8",
        silent_threshold=3,
    ) is False
