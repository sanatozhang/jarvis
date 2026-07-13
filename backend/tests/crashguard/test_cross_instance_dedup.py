"""跨实例去重 + auto-PR 实例闸（2026-06-10 修复）单测。

背景：多机/多实例各用独立 SQLite，本地 DB 去重看不到彼此开的 PR → 同 issue
重复开。修复：开 PR 前查 GitHub 现存 open crashguard PR（权威）+ 非指派实例
（scheduler_enabled=false）跳过 auto-PR。
"""
import json
import subprocess
from types import SimpleNamespace

from app.crashguard.services import pr_drafter as D


def _fake_run_factory(prs, returncode=0, stderr=""):
    def _fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=returncode,
            stdout=json.dumps(prs) if returncode == 0 else "",
            stderr=stderr,
        )
    return _fake_run


def test_github_dedup_hit_matches_issue_branch(monkeypatch):
    monkeypatch.setattr(D, "_github_slug", lambda p: "Plaud-AI/plaud-flutter-common")
    monkeypatch.setattr(subprocess, "run", _fake_run_factory([
        {"headRefName": "crashguard/flutter/29f69d04-202605251013",
         "url": "https://github.com/Plaud-AI/plaud-flutter-common/pull/1207"},
    ]))
    hit = D._github_open_crashguard_pr("/tmp/repo", "29f69d04-49a2-11f1-8751-da7ad0900002")
    assert hit == "https://github.com/Plaud-AI/plaud-flutter-common/pull/1207"


def test_github_dedup_miss_returns_none(monkeypatch):
    monkeypatch.setattr(D, "_github_slug", lambda p: "Plaud-AI/plaud-flutter-common")
    monkeypatch.setattr(subprocess, "run", _fake_run_factory([
        {"headRefName": "crashguard/flutter/aaaaaaaa-202606010101", "url": "x"},
        {"headRefName": "feature/someone/unrelated", "url": "y"},
    ]))
    assert D._github_open_crashguard_pr("/tmp/repo", "29f69d04-xxxx") is None


def test_github_dedup_failopen_on_query_error(monkeypatch):
    """GitHub 查询失败 → 返回 None（fail-open，不卡死自动化）。"""
    monkeypatch.setattr(D, "_github_slug", lambda p: "Plaud-AI/plaud-flutter-common")
    monkeypatch.setattr(subprocess, "run", _fake_run_factory([], returncode=1, stderr="boom"))
    assert D._github_open_crashguard_pr("/tmp/repo", "29f69d04-xxxx") is None


def test_github_dedup_no_slug_returns_none(monkeypatch):
    monkeypatch.setattr(D, "_github_slug", lambda p: "")
    assert D._github_open_crashguard_pr("/tmp/repo", "29f69d04-xxxx") is None


def test_github_dedup_strips_gh_token_env(monkeypatch):
    """2026-07-13 修复：漏剥 GH_TOKEN/GITHUB_TOKEN，过期 fine-grained PAT 挡掉这个
    "GitHub 权威去重"查询时会 fail-open 返回 None——今天实测撞过一次，两条几乎同时
    的开 PR 请求互相查不到对方，各自开了一条重复 PR。"""
    monkeypatch.setenv("GH_TOKEN", "expired-pat")
    monkeypatch.setenv("GITHUB_TOKEN", "expired-pat")
    monkeypatch.setattr(D, "_github_slug", lambda p: "Plaud-AI/plaud-flutter-common")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    D._github_open_crashguard_pr("/tmp/repo", "29f69d04-xxxx")
    assert captured["env"] is not None
    assert "GH_TOKEN" not in captured["env"]
    assert "GITHUB_TOKEN" not in captured["env"]


def test_auto_pr_approvers_constant():
    # auto 入口受实例闸约束；human 不受
    assert "auto" in D._AUTO_PR_APPROVERS
    assert "auto_retry" in D._AUTO_PR_APPROVERS
    assert "top_auto" in D._AUTO_PR_APPROVERS
    assert "human" not in D._AUTO_PR_APPROVERS
