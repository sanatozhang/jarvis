import pytest
from app.crashguard.services.version_classifier import classify_version


TOP = {
    "android": {"version": "3.19.0-634", "users": 12345},
    "ios": {"version": "3.18.0-712", "users": 9876},
}


def test_classify_version_new_when_greater():
    # 3.20.0 > 3.19.0 (主版本) → new
    assert classify_version("3.20.0-700", "android", TOP) == "new"


def test_classify_version_main_when_equal_ignoring_build():
    # 3.19.0-700 vs top 3.19.0-634，忽略 build → main
    assert classify_version("3.19.0-700", "android", TOP) == "main"


def test_classify_version_legacy_when_less():
    assert classify_version("3.16.0-500", "android", TOP) == "legacy"


def test_classify_version_unknown_platform_returns_legacy():
    # 平台不在 top dict → 归 legacy（走大盘桶兜底）
    assert classify_version("3.20.0", "windows", TOP) == "legacy"


def test_classify_version_empty_version_returns_legacy():
    assert classify_version("", "android", TOP) == "legacy"


def test_classify_version_unparseable_returns_legacy():
    assert classify_version("abc-xyz", "android", TOP) == "legacy"


def test_classify_version_empty_top_returns_legacy():
    assert classify_version("3.20.0", "android", {}) == "legacy"


def test_classify_version_top_missing_version_field():
    assert classify_version("3.20.0", "android", {"android": {}}) == "legacy"


def test_classify_version_minor_bump_is_new():
    # 3.19.1 > 3.19.0 → new（patch bump 也算新）
    assert classify_version("3.19.1-650", "android", TOP) == "new"
