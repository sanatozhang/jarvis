"""测试 PR 12 道质量闸门。

每道闸单测 + 与 pr_drafter 接入断言，保证未来重构不退化。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from app.crashguard.services import pr_quality_gates as g


# ============================================================
# Gate#1：路径存在性预校验
# ============================================================
def test_gate1_empty_skip():
    ok, reason, _ = g.verify_fix_paths("/tmp", "", "")
    assert ok
    assert "skipped" in reason


def test_gate1_real_path_in_diff():
    with tempfile.TemporaryDirectory() as td:
        Path(td, "app").mkdir()
        Path(td, "app/main.py").write_text("x = 1\n")
        diff = "--- a/app/main.py\n+++ b/app/main.py\n@@\n+pass\n"
        ok, reason, info = g.verify_fix_paths(td, "", diff)
        assert ok, reason
        assert "app/main.py" in info["existing"]


def test_gate1_phantom_path_blocked():
    with tempfile.TemporaryDirectory() as td:
        diff = "--- a/app/imaginary.kt\n+++ b/app/imaginary.kt\n@@\n+pass\n"
        ok, reason, info = g.verify_fix_paths(td, "", diff, min_ratio=0.5)
        assert not ok
        assert "path_check_failed" in reason
        assert "app/imaginary.kt" in info["missing"]


def test_gate1_basename_rglob_fallback():
    """AI 把 MainActivity.kt 路径写错了目录，但文件名对——basename rglob 应该兜回。"""
    with tempfile.TemporaryDirectory() as td:
        deep = Path(td, "app/src/main/java/ai/plaud/android")
        deep.mkdir(parents=True)
        (deep / "MainActivity.kt").write_text("class MainActivity {}")
        diff = "--- a/MainActivity.kt\n+++ b/MainActivity.kt\n@@\n+pass\n"
        ok, _, info = g.verify_fix_paths(td, "", diff, min_ratio=0.5)
        assert ok, info


# ============================================================
# Gate#2：stack→平台强制路由
# ============================================================
@pytest.mark.parametrize("stack,expected", [
    ("at package:flutter/widgets.dart 234", "flutter"),
    ("crash at lib/foo.dart:42", "flutter"),
    ("FlutterEngine.cpp:99", "flutter"),
    ("at MainActivity.kt:55", "android"),
    ("java.lang.NullPointerException", "android"),
    ("at AppDelegate.swift:120", "ios"),
    ("Swift.Optional<Foo>", "ios"),
    ("NSInvalidArgumentException raised", "ios"),
    ("", None),
    ("totally unrelated text", None),
])
def test_gate2_force_route(stack, expected):
    fp, _ = g.detect_forced_platform(stack, "android")
    assert fp == expected, f"stack={stack!r} got={fp} expected={expected}"


# ============================================================
# Gate#3：confidence/feasibility 门槛
# ============================================================
def test_gate3_pass():
    ok, _ = g.pass_confidence_gate("high", 0.85)
    assert ok


def test_gate3_low_confidence_blocked():
    ok, why = g.pass_confidence_gate("medium", 0.85)
    assert not ok
    assert "confidence_too_low" in why


def test_gate3_low_feasibility_blocked():
    ok, why = g.pass_confidence_gate("high", 0.5)
    assert not ok
    assert "feasibility_too_low" in why


# ============================================================
# Gate#5：预投喂实存文件清单
# ============================================================
def test_gate5_finds_files_from_diff_and_text():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td, "app/src/main/java/ai/plaud")
        d.mkdir(parents=True)
        (d / "MainActivity.kt").write_text("class MainActivity {}")
        (d / "SplashActivity.kt").write_text("class SplashActivity {}")
        # fix_suggestion 只提"修复 MainActivity 中..."不含路径
        files = g.collect_existing_paths_for_keywords(
            td, "修复 `MainActivity` 中的 onWindowStartingActionMode", "", max_n=10,
        )
        assert any("MainActivity.kt" in f for f in files), files


# ============================================================
# Gate#7：语法速检
# ============================================================
def test_gate7_python_syntax():
    with tempfile.TemporaryDirectory() as td:
        Path(td, "good.py").write_text("x = 1\n")
        Path(td, "bad.py").write_text("def broken(\n")
        ok, _, info = g.lint_changed_files(td, ["good.py"])
        assert ok
        ok, why, info = g.lint_changed_files(td, ["bad.py"])
        assert not ok
        assert "py_compile" in (info.get("errors") or [""])[0]


def test_gate7_nonexistent_skipped():
    with tempfile.TemporaryDirectory() as td:
        ok, _, info = g.lint_changed_files(td, ["does_not_exist.kt"])
        assert ok
        assert info["checked"] == []


# ============================================================
# Gate#8：关键词命中
# ============================================================
def test_gate8_hit():
    ok, _, info = g.verify_keyword_hits(
        "+ override fun onWindowStartingActionMode() {}",
        "需要 override `onWindowStartingActionMode` 在 MainActivity 中",
    )
    assert ok
    assert "onWindowStartingActionMode" in info["hits"]


def test_gate8_miss():
    ok, why, info = g.verify_keyword_hits(
        "+ val x = 1",
        "需要 override `onWindowStartingActionMode` 在 MainActivity 中",
    )
    assert not ok
    assert "keyword_hit_failed" in why


def test_gate8_empty_suggestion_skipped():
    ok, _, _ = g.verify_keyword_hits("+ val x = 1", "")
    assert ok


# ============================================================
# Gate#10：多候选合议
# ============================================================
def test_gate10_force_flutter_when_dart_in_stack():
    cands = [("android", "/a"), ("flutter", "/f")]
    primary, why = g.pick_primary_platform(
        cands, "at package:flutter/foo.dart:1", "", "android",
    )
    assert primary == ("flutter", "/f")
    assert "forced_by_stack" in why


def test_gate10_claimed_platform_first():
    cands = [("flutter", "/f"), ("android", "/a")]
    primary, why = g.pick_primary_platform(
        cands, "", "", "android",
    )
    assert primary == ("android", "/a")
    assert "claimed_platform" in why


def test_gate10_empty_candidates():
    primary, why = g.pick_primary_platform([], "", "", "android")
    assert primary is None
    assert why == "no_candidates"


# ============================================================
# Gate#12：CI verdict 推导
# ============================================================
def test_gate12_ci_pass():
    from app.crashguard.services.pr_sync import _derive_ci_verdict
    payload = {"statusCheckRollup": [
        {"name": "build", "conclusion": "SUCCESS"},
        {"name": "test", "state": "SUCCESS"},
    ]}
    v, names = _derive_ci_verdict(payload)
    assert v == "pass"
    assert names == []


def test_gate12_ci_fail():
    from app.crashguard.services.pr_sync import _derive_ci_verdict
    payload = {"statusCheckRollup": [
        {"name": "build", "conclusion": "SUCCESS"},
        {"name": "test", "conclusion": "FAILURE"},
    ]}
    v, names = _derive_ci_verdict(payload)
    assert v == "fail"
    assert "test" in names


def test_gate12_ci_pending():
    from app.crashguard.services.pr_sync import _derive_ci_verdict
    payload = {"statusCheckRollup": [
        {"name": "build", "conclusion": "SUCCESS"},
        {"name": "lint", "status": "IN_PROGRESS"},
    ]}
    v, _ = _derive_ci_verdict(payload)
    assert v == "pending"


def test_gate12_ci_none():
    from app.crashguard.services.pr_sync import _derive_ci_verdict
    v, names = _derive_ci_verdict({})
    assert v == "none"
    assert names == []
