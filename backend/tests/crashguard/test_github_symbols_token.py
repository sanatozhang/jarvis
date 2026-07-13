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
