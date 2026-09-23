"""github_symbols.py::_github_token() 单测。

背景（2026-07-13）：_github_token() 原来直读 GH_TOKEN/GITHUB_TOKEN env，
这俩存的是个人 fine-grained PAT，超过 Plaud-AI org 90 天生命周期策略会被
硬拒绝（release 列表接口全 403）。第一版修复是"优先 `gh auth token`，
env 作兜底"。

**2026-08-06 起 env 兜底被彻底删掉**（见 `_github_token` 的 docstring）：
兜底回一个必定 403 的过期 PAT，比干脆没有 token 更糟——没 token 时走匿名
请求，公开 release 反而下得下来；带一个过期 PAT 则是直接 403。本文件里
原来那两条 `falls_back_to_env` 用例断言的正是被删掉的行为，从那之后一直
红着，现在改成钉住"**不**兜底"这个性质。
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


def test_github_token_does_not_fall_back_to_env_when_gh_fails(monkeypatch):
    """`gh auth token` 失败 → 返回 None，**不**回落 env 里的 PAT。

    回落一个必定 403 的过期 PAT 比没有 token 更糟：没 token 走匿名请求，
    公开 release 还下得下来；带过期 PAT 直接 403。
    """
    from app.crashguard.services import github_symbols as G

    monkeypatch.setenv("GH_TOKEN", "expired-pat")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="not logged in")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert G._github_token() is None


def test_github_token_does_not_fall_back_to_env_when_gh_missing(monkeypatch):
    """机器上没装 `gh` → 同样返回 None，理由同上。"""
    from app.crashguard.services import github_symbols as G

    monkeypatch.setenv("GITHUB_TOKEN", "expired-pat")
    monkeypatch.delenv("GH_TOKEN", raising=False)

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("gh not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert G._github_token() is None


def test_github_token_ignores_env_even_when_gh_returns_empty(monkeypatch):
    """`gh` 退出码 0 但吐空串（登录态坏了的一种形态）也不能回落 env。"""
    from app.crashguard.services import github_symbols as G

    monkeypatch.setenv("GH_TOKEN", "expired-pat")

    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=0, stdout="  \n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert G._github_token() is None


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
