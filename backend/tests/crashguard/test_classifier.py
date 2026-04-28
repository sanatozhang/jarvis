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
