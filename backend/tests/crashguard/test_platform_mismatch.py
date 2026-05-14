"""Unit tests for analyzer._detect_platform_mismatch — Gate#2 前移到 analyzer。"""
from __future__ import annotations

from app.crashguard.services.analyzer import _detect_platform_mismatch


def test_flutter_diff_with_swift_path_caught():
    diff = (
        "--- a/ios/Runner/LoginVC.swift\n"
        "+++ b/ios/Runner/LoginVC.swift\n"
        "@@ -1,3 +1,3 @@\n"
        "-foo\n"
        "+bar\n"
    )
    reason = _detect_platform_mismatch(
        platform="flutter", fix_suggestion="", fix_diff=diff, solution="",
    )
    assert ".swift" in reason


def test_ios_diff_with_dart_path_caught():
    diff = "--- a/lib/foo.dart\n+++ b/lib/foo.dart\n@@ -1,1 +1,1 @@\n-a\n+b\n"
    reason = _detect_platform_mismatch(
        platform="ios", fix_suggestion="", fix_diff=diff, solution="",
    )
    assert ".dart" in reason


def test_flutter_diff_with_dart_passes():
    diff = "--- a/lib/foo.dart\n+++ b/lib/foo.dart\n@@ -1,1 +1,1 @@\n-a\n+b\n"
    reason = _detect_platform_mismatch(
        platform="flutter", fix_suggestion="", fix_diff=diff, solution="",
    )
    assert reason == ""


def test_empty_diff_does_not_block():
    """complexity=complex 时 fix_diff 为空，不应被拦截。"""
    reason = _detect_platform_mismatch(
        platform="flutter",
        fix_suggestion="建议在 swift 层 catch 异常",  # 自然语言提到 swift 不阻断
        fix_diff="",
        solution="",
    )
    assert reason == ""


def test_unknown_platform_falls_through():
    """platform 不在白名单（如 web）时不阻断。"""
    diff = "--- a/foo.swift\n+++ b/foo.swift\n"
    reason = _detect_platform_mismatch(
        platform="web", fix_suggestion="", fix_diff=diff, solution="",
    )
    assert reason == ""
