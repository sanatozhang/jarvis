"""集成测试：Gate#8b（fix_diff 为空时强制过二级 LLM 判官）是否被正确接入
`draft_pr_for_analysis`。

跟 `tests/crashguard/test_pr_quality_gates.py` 里的 `judge_diff_with_llm` 单测
不同——那些测试只验证 `judge_diff_with_llm` 函数本身在给定输入下的评分行为，
完全没有触碰 `pr_drafter.py::draft_pr_for_analysis` 里新增的 Gate#8b 调用逻辑。
mutation test 已证实：把 Gate#8b 那段代码整个删掉，仅保留
`fix_diff_was_empty = not (ana.fix_diff or "").strip()` 这行赋值，
`pytest tests/crashguard/ -q` 依然全绿——因为没有任何测试断言过"这段代码是否
真的被调用"。

本文件跑真实的 `draft_pr_for_analysis` 流程（真实本地 git repo + 真实 git
fetch/checkout/commit/push，只 mock 掉 `judge_diff_with_llm` 本身、implementation
agent 和跟 GitHub 的耦合面），直接断言 mock 的调用次数/参数，而不是只看某个
纯函数的返回值：

  1. `ana.fix_diff` 为空 + mock 的 `judge_diff_with_llm` 判低分
     → `draft_pr_for_analysis` 最终 `ok=False`，`error` 前缀
       `gate_empty_fix_diff_judge`，且 `judge_diff_with_llm` 被调用了恰好一次。
  2. `ana.fix_diff` 非空（正常场景）
     → `judge_diff_with_llm` 完全不会被调用（调用次数为 0），确认 Gate#8b
       不会误伤有真实 fix_diff 的正常 PR。
"""
from __future__ import annotations

import subprocess as _sp
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.asyncio


PR1067_ROOT_CAUSE = (
    "根因是 NiceBuildApplication.onCreate() 通过 AppInitFailureManager.runCatchingFlutterInit "
    "同步调用 initFlutterEngine()，这段同步耗时操作发生在 Application.onCreate() 生命周期内"
    "触发了 ANR。"
)
PR1067_FIX_SUGGESTION = (
    "建议把 initFlutterEngine() 的调用从 onCreate() 移到异步线程，避免阻塞 onCreate()。"
)


def _init_repo_with_remote(tmp_path: Path) -> str:
    """建一个带真实 origin 远端（本地 bare repo）的最小 git 仓库。

    `draft_pr_for_analysis` 进入"真实操作"分支后第一件事就是 `git fetch <remote>`
    + 解析 `origin/main` 作为 base_ref——裸的 `git init`（无远端）会在 Gate#8b
    之前就因为 fetch 失败而早退，所以这里必须建一个真正可 fetch/push 的远端。
    """
    work = tmp_path / "work"
    work.mkdir()
    _sp.run(["git", "init", "-q", "-b", "main"], cwd=str(work), check=True)
    _sp.run(["git", "config", "user.email", "t@t"], cwd=str(work), check=True)
    _sp.run(["git", "config", "user.name", "t"], cwd=str(work), check=True)
    (work / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
    _sp.run(["git", "add", "-A"], cwd=str(work), check=True)
    _sp.run(["git", "commit", "-qm", "init"], cwd=str(work), check=True)

    bare = tmp_path / "origin.git"
    _sp.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _sp.run(["git", "remote", "add", "origin", str(bare)], cwd=str(work), check=True)
    _sp.run(["git", "push", "-q", "-u", "origin", "main"], cwd=str(work), check=True)
    return str(work)


def _make_settings(**overrides) -> SimpleNamespace:
    """最小化 gate 组合：只留 Gate#8b 相关行为可观察，其余 gate 关掉减少测试面。"""
    base = dict(
        pr_enabled=True,
        scheduler_enabled=True,
        pr_dedup_days=30,
        frontend_base_url="http://fe.example",
        gate_confidence_enabled=False,
        gate_force_route_enabled=False,
        gate_path_verify_enabled=False,
        gate_keyword_enabled=False,
        gate_syntax_enabled=False,
        gate_llm_judge_enabled=False,   # 保持默认关——Gate#8b 应不受这个开关约束
        gate_llm_judge_min_score=7,
        gate_primary_only_enabled=True,
        pr_create_as_draft=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


async def _make_issue_and_analysis(tmp_path, issue_id: str, *, fix_diff: str, monkeypatch) -> int:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / f'{issue_id}.db'}")
    from app.db.database import get_session, init_db
    from app.crashguard import models  # noqa: F401
    from app.crashguard.models import CrashAnalysis, CrashIssue

    await init_db()
    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id=issue_id,
            platform="android",
            title="ANR in onCreate",
            top_app_version="3.2.0",
            top_os="",
            # 含 ".kt" 满足 pr_drafter._stack_matches_platform 的 android 白名单，
            # 不然会在 Gate#8b 之前就被 stack_mismatch 拦下。
            representative_stack="at NiceBuildApplication.onCreate(NiceBuildApplication.kt:42)",
        ))
        ana = CrashAnalysis(
            datadog_issue_id=issue_id,
            analysis_run_id=f"run-{issue_id}",
            status="success",
            followup_question="",
            root_cause=PR1067_ROOT_CAUSE,
            fix_suggestion=PR1067_FIX_SUGGESTION,
            fix_diff=fix_diff,
            confidence="high",
            feasibility_score=0.9,
        )
        session.add(ana)
        await session.commit()
        analysis_id = ana.id
    return analysis_id


def _patch_common(monkeypatch, pr_drafter, *, settings: SimpleNamespace):
    """公共 mock：settings / repo 路由 / audit / gh 命令。"""
    monkeypatch.setattr(pr_drafter, "get_crashguard_settings", lambda: settings)
    monkeypatch.setattr(pr_drafter.repo_router, "resolve", lambda *a, **kw: None)
    monkeypatch.setattr(pr_drafter, "get_repo_routing", lambda: {})
    monkeypatch.setattr("app.crashguard.services.audit.write_audit", AsyncMock())

    # 沙盒里没有真实 GitHub 远端/凭据——把 `gh ...` 命令拦下来返回一个确定性失败，
    # 其余 `git ...` 命令原样跑真实 subprocess（真实 fetch/checkout/commit/push）。
    # 不 mock 这一步会让测试依赖沙盒是否装了 gh CLI / 是否联网，不确定性太高。
    orig_run_git = pr_drafter._run_git

    def _run_git_no_gh(cmd, cwd, timeout=60):
        if cmd and cmd[0] == "gh":
            return 1, "", "gh disabled in test sandbox"
        return orig_run_git(cmd, cwd, timeout=timeout)

    monkeypatch.setattr(pr_drafter, "_run_git", _run_git_no_gh)


async def test_gate8b_blocks_when_fix_diff_empty_and_judge_rejects(tmp_path, monkeypatch):
    """核心场景 1：fix_diff 为空 + 判官判低分 → Gate#8b 拦截，且判官确实被调用了一次。

    这是能证明"Gate#8b 这段新代码真的接入了 draft_pr_for_analysis"的测试——
    删掉 Gate#8b 那段代码后，这个测试必须失败（已用 mutation test 验证，见
    task-6-report.md）。
    """
    from app.crashguard.services import pr_drafter

    repo_path = _init_repo_with_remote(tmp_path)
    analysis_id = await _make_issue_and_analysis(
        tmp_path, "ddi_gate8b_blocked", fix_diff="", monkeypatch=monkeypatch,
    )
    _patch_common(monkeypatch, pr_drafter, settings=_make_settings())

    # implementation agent：模拟"自由发挥"——真实改了仓库里的 a.txt，
    # 不需要真跑 Claude/Codex CLI。
    async def _fake_impl_agent(repo_path_arg, ana, issue):
        (Path(repo_path_arg) / "a.txt").write_text("hello\nWORLD_FREEFORM\n", encoding="utf-8")
        return True, ["a.txt"], {}, ""

    monkeypatch.setattr(pr_drafter, "_run_implementation_agent", _fake_impl_agent)

    judge_mock = AsyncMock(return_value=(
        False, "llm_judge_failed: score=2/7 verdict=reject reason=unrelated diff",
        {"score": 2, "verdict": "reject"},
    ))
    monkeypatch.setattr(
        "app.crashguard.services.pr_quality_gates.judge_diff_with_llm", judge_mock,
    )

    result = await pr_drafter.draft_pr_for_analysis(
        analysis_id, approver="human", repo_override=("android", repo_path),
    )

    assert result["ok"] is False
    assert result["error"].startswith("gate_empty_fix_diff_judge:"), result
    assert judge_mock.call_count == 1, (
        f"Gate#8b 应该在 fix_diff 为空时调用 judge_diff_with_llm 恰好一次，"
        f"实际调用了 {judge_mock.call_count} 次"
    )
    # diff_text（第一个位置参数）必须真的把 implementation agent 的改动传进去
    called_diff_text = judge_mock.call_args.args[0]
    assert "WORLD_FREEFORM" in called_diff_text


async def test_gate8b_not_called_when_fix_diff_present(tmp_path, monkeypatch):
    """核心场景 2：fix_diff 非空（正常场景）→ Gate#8b 完全不应该调用判官。

    确认新增的强制判官分支只影响"fix_diff 为空"这个高风险子集，不影响其余
    68% 有真实 fix_diff 的正常 PR（不误杀 + 不多花一次 agent 调用成本）。

    不断言最终 `result["ok"]`——沙盒里没有真实 GitHub 远端，`gh pr create`
    这一步大概率会失败，这是预期内、无关紧要的；本测试只关心 Gate#8b 有没有
    被误触发。
    """
    from app.crashguard.services import pr_drafter

    repo_path = _init_repo_with_remote(tmp_path)
    real_fix_diff = (
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1,2 +1,2 @@\n"
        " hello\n"
        "-world\n"
        "+WORLD\n"
    )
    analysis_id = await _make_issue_and_analysis(
        tmp_path, "ddi_gate8b_skip", fix_diff=real_fix_diff, monkeypatch=monkeypatch,
    )
    _patch_common(monkeypatch, pr_drafter, settings=_make_settings())

    # implementation agent 跳过（模拟未部署/失败），落到"优先 2"旧路径：
    # 直接 git apply ana.fix_diff。
    async def _fake_impl_agent_fail(repo_path_arg, ana, issue):
        return False, [], {}, "impl agent skipped for test"

    monkeypatch.setattr(pr_drafter, "_run_implementation_agent", _fake_impl_agent_fail)

    judge_mock = AsyncMock(return_value=(True, "llm_judge_ok: score=9 verdict=approve", {"score": 9}))
    monkeypatch.setattr(
        "app.crashguard.services.pr_quality_gates.judge_diff_with_llm", judge_mock,
    )

    result = await pr_drafter.draft_pr_for_analysis(
        analysis_id, approver="human", repo_override=("android", repo_path),
    )

    assert judge_mock.call_count == 0, (
        f"Gate#8b 不应该在 fix_diff 非空时调用 judge_diff_with_llm，"
        f"但实际被调用了 {judge_mock.call_count} 次：{judge_mock.call_args_list}"
    )
    assert result.get("error") != "gate_empty_fix_diff_judge"
