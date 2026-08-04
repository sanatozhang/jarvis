"""测试 PR 12 道质量闸门。

每道闸单测 + 与 pr_drafter 接入断言，保证未来重构不退化。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

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
# Gate#8：关键词命中（min_hits 默认 1 → 2，关键词来源 fix_suggestion → root_cause+fix_suggestion）
# ============================================================
def test_gate8_hit():
    """min_hits 默认提到 2 后，fixture 需至少命中 2 个才算真实命中场景。"""
    ok, _, info = g.verify_keyword_hits(
        "+ override fun onWindowStartingActionMode() {}\n"
        "+ class MainActivity {}",
        "需要 override `onWindowStartingActionMode` 在 `MainActivity` 中",
    )
    assert ok
    assert "onWindowStartingActionMode" in info["hits"]
    assert "MainActivity" in info["hits"]


def test_gate8_miss():
    ok, why, info = g.verify_keyword_hits(
        "+ val x = 1",
        "需要 override `onWindowStartingActionMode` 在 MainActivity 中",
    )
    assert not ok
    assert "keyword_hit_failed" in why


def test_gate8_no_keywords_extractable_blocks():
    """抽不到任何关键词时不再默认放行——无法验证相关性，按不通过处理，交给人工审核。"""
    ok, why, info = g.verify_keyword_hits("+ val x = 1", "")
    assert not ok
    assert "no_keywords_extractable" in why


def test_gate8_min_hits_two_requires_two_matches():
    """新默认值 min_hits=2：只命中 1 个关键词不通过，命中 2 个才通过。"""
    fix = "需要 override `onWindowStartingActionMode` 和 `MainActivity`"
    diff_one_hit = "+ override fun onWindowStartingActionMode() {}"
    ok, why, info = g.verify_keyword_hits(diff_one_hit, fix)
    assert not ok
    assert "keyword_hit_failed" in why
    assert len(info["hits"]) == 1

    diff_two_hits = (
        "+ override fun onWindowStartingActionMode() {}\n"
        "+ class MainActivity {}"
    )
    ok2, _, info2 = g.verify_keyword_hits(diff_two_hits, fix)
    assert ok2
    assert len(info2["hits"]) >= 2


def test_gate8_keywords_combine_root_cause_and_fix_suggestion():
    """新签名：root_cause 里独有的标识符也应被抽取并参与命中判断。"""
    diff = (
        "+ fun uniqueRootCauseMethod() {}\n"
        "+ override fun onWindowStartingActionMode() {}"
    )
    ok, _, info = g.verify_keyword_hits(
        diff,
        "需要 override `onWindowStartingActionMode`",
        root_cause="根因是 `uniqueRootCauseMethod` 未处理异常",
    )
    assert ok
    assert "uniqueRootCauseMethod" in info["keywords"]
    assert "uniqueRootCauseMethod" in info["hits"]


# ============================================================
# Gate#9：二级 LLM 判官（judge_diff_with_llm）
# ============================================================

async def test_gate9_judge_missing_diff_blocks():
    """diff_text 为空 → 无法验证相关性，不再默认放行。"""
    ok, why, info = await g.judge_diff_with_llm("", "some fix", "some cause")
    assert not ok
    assert "missing_diff_or_fix_suggestion" in why
    assert info["score"] is None


async def test_gate9_judge_missing_fix_suggestion_blocks():
    ok, why, info = await g.judge_diff_with_llm("some diff", "", "some cause")
    assert not ok
    assert "missing_diff_or_fix_suggestion" in why


class _DummyJudgeAgent:
    """模拟 AgentOrchestrator 选出的 agent：把预设 verdict 写进 workspace/output/result.json。"""

    def __init__(self, score: int, verdict: str, reason: str = "mock reason"):
        self._score = score
        self._verdict = verdict
        self._reason = reason

    async def analyze(self, workspace, prompt):
        out_dir = Path(workspace) / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "result.json").write_text(
            json.dumps({"score": self._score, "verdict": self._verdict, "reason": self._reason}),
            encoding="utf-8",
        )


class _DummyJudgeOrch:
    def __init__(self, agent):
        self._agent = agent

    def select_agent(self, **_):
        return self._agent


def _patch_judge_agent(monkeypatch, score: int, verdict: str, reason: str = "mock reason"):
    import app.services.agent_orchestrator as _ao
    agent = _DummyJudgeAgent(score, verdict, reason)
    monkeypatch.setattr(_ao, "AgentOrchestrator", lambda: _DummyJudgeOrch(agent))


@pytest.mark.asyncio
async def test_gate9_judge_approves_high_score(monkeypatch):
    _patch_judge_agent(monkeypatch, score=8, verdict="approve")
    ok, why, info = await g.judge_diff_with_llm(
        "+ some diff", "some fix suggestion", "some root cause", min_score=7,
    )
    assert ok, why
    assert info["score"] == 8


@pytest.mark.asyncio
async def test_gate9_judge_rejects_low_score(monkeypatch):
    _patch_judge_agent(monkeypatch, score=2, verdict="reject")
    ok, why, info = await g.judge_diff_with_llm(
        "+ some diff", "some fix suggestion", "some root cause", min_score=7,
    )
    assert not ok
    assert info["score"] == 2


# ============================================================
# judge_diff_with_llm 的 PR #1067 风格 fixture（root_cause/fix_suggestion 描述
# FlutterEngine ANR，diff 要么是无关的 IsarMigrationManager.kt、要么是真的改对了）。
#
# ⚠️ 这两个测试只验证 `judge_diff_with_llm` 函数本身在这类输入下的评分行为
# （给低分→ok=False，给高分→ok=True）——跟 test_gate9_judge_rejects_low_score /
# test_gate9_judge_approves_high_score 是同一件事，只是换了更贴近真实场景的文本。
#
# 它们**不**验证 pr_drafter.py 里 Gate#8b 这段新增调用逻辑是否被正确接入
# draft_pr_for_analysis（即"fix_diff 为空时是否真的会触发这次强制判官调用、
# fix_diff 非空时是否真的不会调用"）——那部分由
# tests/crashguard/test_pr_drafter_gate8b.py 里的集成测试覆盖（mock
# judge_diff_with_llm 断言调用次数，而不是只看 judge_diff_with_llm 自身返回值）。
# ============================================================

PR_1067_ROOT_CAUSE = (
    "根因是 NiceBuildApplication.onCreate() 通过 AppInitFailureManager.runCatchingFlutterInit "
    "同步调用 initFlutterEngine() → flutterEngine.dartExecutor.executeDartEntrypoint()，"
    "这段同步耗时操作发生在 Application.onCreate() 生命周期内触发了 ANR。"
)
PR_1067_FIX_SUGGESTION = (
    "建议把 initFlutterEngine() 的调用从 onCreate() 移到异步线程或延后到首帧渲染后，"
    "避免阻塞 Application.onCreate()。"
)
PR_1067_UNRELATED_DIFF = (
    "--- a/app/src/main/java/ai/plaud/android/plaud/storage/db/migration/IsarMigrationManager.kt\n"
    "+++ b/app/src/main/java/ai/plaud/android/plaud/storage/db/migration/IsarMigrationManager.kt\n"
    "@@ -10,0 +11,13 @@\n"
    "+    fun migrateSchema(db: IsarDatabase) {\n"
    "+        db.beginTransaction()\n"
    "+        // ... 13 行无关的数据库迁移代码\n"
    "+    }\n"
)


@pytest.mark.asyncio
async def test_judge_diff_with_llm_pr1067_fixture_unrelated_diff_scores_low(monkeypatch):
    """judge_diff_with_llm 本身在 PR #1067 风格输入下的评分行为（低分场景）。

    root_cause/fix_suggestion 描述 FlutterEngine ANR，diff 却是与根因完全无关的
    IsarMigrationManager.kt。mock 判官给出低分/reject，断言 judge_diff_with_llm
    返回 ok=False。

    ⚠️ 这只验证 judge_diff_with_llm 函数自身的评分行为——是否真的被 pr_drafter.py
    的 Gate#8b 在 fix_diff 为空时正确调用，由
    tests/crashguard/test_pr_drafter_gate8b.py 的集成测试断言（mock 调用次数），
    不是这个测试的职责。
    """
    _patch_judge_agent(
        monkeypatch, score=2, verdict="reject",
        reason="diff 改的是 IsarMigrationManager 数据库迁移代码，与 FlutterEngine ANR 根因完全无关",
    )
    ok, why, info = await g.judge_diff_with_llm(
        PR_1067_UNRELATED_DIFF, PR_1067_FIX_SUGGESTION, PR_1067_ROOT_CAUSE, min_score=7,
    )
    assert not ok, why
    assert info["score"] == 2


@pytest.mark.asyncio
async def test_judge_diff_with_llm_pr1067_fixture_genuine_fix_scores_high(monkeypatch):
    """judge_diff_with_llm 本身在 PR #1067 风格输入下的评分行为（不误杀场景）。

    这次 diff 确实改对了地方（把 initFlutterEngine 移到异步线程，命中根因描述）。
    mock 判官给高分/approve，断言 judge_diff_with_llm 返回 ok=True——确认判官
    本身不会把"自由发挥但改对了"的场景也打低分。
    """
    genuine_diff = (
        "--- a/app/src/main/java/ai/plaud/android/plaud/NiceBuildApplication.kt\n"
        "+++ b/app/src/main/java/ai/plaud/android/plaud/NiceBuildApplication.kt\n"
        "@@ -20,7 +20,11 @@\n"
        "     override fun onCreate() {\n"
        "         super.onCreate()\n"
        "-        initFlutterEngine()\n"
        "+        GlobalScope.launch(Dispatchers.Default) {\n"
        "+            initFlutterEngine()\n"
        "+        }\n"
        "     }\n"
    )
    _patch_judge_agent(
        monkeypatch, score=8, verdict="approve",
        reason="将 initFlutterEngine 移到异步线程执行，符合 fix_suggestion 描述的修复方向",
    )
    ok, why, info = await g.judge_diff_with_llm(
        genuine_diff, PR_1067_FIX_SUGGESTION, PR_1067_ROOT_CAUSE, min_score=7,
    )
    assert ok, why
    assert info["score"] == 8


# ============================================================
# PR body 措辞纠偏（_build_pr_body）
# ============================================================

def _make_pr_body_fixtures():
    issue = SimpleNamespace(
        platform="android", datadog_issue_id="iss-1", title="ANR in onCreate",
    )
    ana = SimpleNamespace(
        confidence="high", feasibility_score=0.9,
        root_cause="根因描述", fix_suggestion="修复建议", solution=None,
        fix_diff="",
    )
    return issue, ana


def test_build_pr_body_patch_applied_with_real_fix_diff():
    from app.crashguard.services.pr_drafter import _build_pr_body
    issue, ana = _make_pr_body_fixtures()
    ana.fix_diff = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
    body = _build_pr_body(issue, ana, "http://fe", patch_applied=True, fix_diff_was_empty=False)
    assert "AI 已落 patch 到代码" in body


def test_build_pr_body_patch_applied_but_fix_diff_was_empty():
    from app.crashguard.services.pr_drafter import _build_pr_body
    issue, ana = _make_pr_body_fixtures()
    body = _build_pr_body(issue, ana, "http://fe", patch_applied=True, fix_diff_was_empty=True)
    assert "未在分析阶段预先给出 diff" in body
    assert "Files changed 即为修复 diff" not in body


def test_build_pr_body_not_patch_applied_unaffected():
    from app.crashguard.services.pr_drafter import _build_pr_body
    issue, ana = _make_pr_body_fixtures()
    body = _build_pr_body(issue, ana, "http://fe", patch_applied=False, fix_diff_was_empty=False)
    assert "未自动 patch 代码" in body


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
