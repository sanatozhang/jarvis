"""github_symbols.py::_github_token() 单测（2026-07-13）。

背景：_github_token() 原来直读 GH_TOKEN/GITHUB_TOKEN env，这俩存的是个人
fine-grained PAT，超过 Plaud-AI org 90 天生命周期策略会被硬拒绝（release 列表
接口全 403）。修复：优先问 `gh auth token` 要服务器上已登录的 OAuth token，
env 只作 gh 不可用时的兜底。
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest


def test_github_token_prefers_gh_auth_token(monkeypatch):
    from app.crashguard.services import github_symbols as G

    monkeypatch.setenv("GH_TOKEN", "expired-pat")

    def fake_run(cmd, **kwargs):
        assert cmd == ["gh", "auth", "token"]
        return SimpleNamespace(returncode=0, stdout="gho_liveoauthtoken\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert G._github_token() == "gho_liveoauthtoken"


def test_github_token_falls_back_to_env_when_gh_fails(monkeypatch):
    from app.crashguard.services import github_symbols as G

    monkeypatch.setenv("GH_TOKEN", "expired-pat")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="not logged in")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert G._github_token() == "expired-pat"


def test_github_token_falls_back_to_env_when_gh_missing(monkeypatch):
    from app.crashguard.services import github_symbols as G

    monkeypatch.setenv("GITHUB_TOKEN", "expired-pat")
    monkeypatch.delenv("GH_TOKEN", raising=False)

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("gh not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert G._github_token() == "expired-pat"


def test_github_token_strips_gh_token_env_before_invoking_gh(monkeypatch):
    """2026-07-20 修复：_github_token() 调 `gh auth token` 时未剥离
    GH_TOKEN/GITHUB_TOKEN env——真实的 `gh` 二进制会尊重这两个 env var，
    于是又把过期 fine-grained PAT 取了回来，102 上实测所有 release 下载
    403（org 90 天生命周期策略拒绝）。此前的 mock（本文件其余用例）直接
    返回假 token，没有模拟"真实 gh 读 env"这个行为，所以没测出这个坑。
    这里断言传给 subprocess.run 的 env 里不含这两个 key（与
    test_cross_instance_dedup.py::test_github_dedup_strips_gh_token_env
    同款断言风格）。
    """
    from app.crashguard.services import github_symbols as G

    monkeypatch.setenv("GH_TOKEN", "expired-pat")
    monkeypatch.setenv("GITHUB_TOKEN", "expired-pat")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return SimpleNamespace(returncode=0, stdout="gho_liveoauthtoken\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert G._github_token() == "gho_liveoauthtoken"
    assert captured["env"] is not None
    assert "GH_TOKEN" not in captured["env"]
    assert "GITHUB_TOKEN" not in captured["env"]
