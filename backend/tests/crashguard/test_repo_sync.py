"""Tests for crashguard.services.repo_sync."""
from __future__ import annotations

import pytest


def test_collect_repo_paths_covers_android_ios_bands_only(monkeypatch):
    from app.crashguard.services import repo_sync

    fake_routing = {
        "android": {"bands": [
            {"min_version": "0", "family": "flutter", "wrapper": "/tmp/plaud_ai", "sub": "plaud-android"},
            {"min_version": "4.0.0", "family": "native", "wrapper": "/tmp/plaud-native-app", "sub": "plaud-native-android"},
        ]},
        "ios": {"bands": [
            {"min_version": "0", "family": "flutter", "wrapper": "/tmp/plaud_ai", "sub": "plaud-ios"},
            {"min_version": "4.0.0", "family": "native", "wrapper": "/tmp/plaud-native-app", "sub": "plaud-native-ios"},
        ]},
        "web": {"bands": [
            {"min_version": "0", "family": "web", "wrapper": "/tmp/plaud-web", "sub": ""},
        ]},
    }
    monkeypatch.setattr(repo_sync, "get_repo_routing", lambda: fake_routing)

    paths = repo_sync._collect_repo_paths()

    assert "/tmp/plaud_ai/plaud-android" in paths
    assert "/tmp/plaud-native-app/plaud-native-android" in paths
    assert "/tmp/plaud_ai/plaud-ios" in paths
    assert "/tmp/plaud-native-app/plaud-native-ios" in paths
    # web 不在 crashguard 监控范围内，不应该出现
    assert not any("plaud-web" in p for p in paths)
    assert len(paths) == len(set(paths))  # 去重


def test_collect_repo_paths_dedupes_shared_wrapper(monkeypatch):
    """android/ios 共用同一个 flutter wrapper + 同一个 sub 时应只出现一次。"""
    from app.crashguard.services import repo_sync

    fake_routing = {
        "android": {"bands": [
            {"min_version": "0", "family": "flutter", "wrapper": "/tmp/plaud_ai", "sub": "plaud-android"},
        ]},
        "ios": {"bands": [
            {"min_version": "0", "family": "flutter", "wrapper": "/tmp/plaud_ai", "sub": "plaud-android"},
        ]},
    }
    monkeypatch.setattr(repo_sync, "get_repo_routing", lambda: fake_routing)

    paths = repo_sync._collect_repo_paths()
    assert paths == ["/tmp/plaud_ai/plaud-android"]


@pytest.mark.asyncio
async def test_sync_one_repo_normal_path(monkeypatch, tmp_path):
    from app.crashguard.services import repo_sync

    repo_path = str(tmp_path)
    monkeypatch.setattr(repo_sync, "_resolve_remote_name", lambda p: "origin")
    monkeypatch.setattr(repo_sync, "_default_base_ref", lambda p: "origin/main")

    call_log = []

    def fake_run_git(cmd, cwd, timeout=60):
        call_log.append(cmd)
        return 0, "", ""

    monkeypatch.setattr(repo_sync, "_run_git", fake_run_git)

    result = await repo_sync._sync_one_repo(repo_path)
    assert result["ok"] is True
    assert result["forced"] is False
    # normal path: fetch, checkout, pull --ff-only — no force/reset commands issued
    assert call_log == [
        ["git", "fetch", "origin"],
        ["git", "checkout", "main"],
        ["git", "pull", "--ff-only", "origin", "main"],
    ]


@pytest.mark.asyncio
async def test_sync_one_repo_falls_back_to_force_reset(monkeypatch, tmp_path):
    from app.crashguard.services import repo_sync

    repo_path = str(tmp_path)
    monkeypatch.setattr(repo_sync, "_resolve_remote_name", lambda p: "origin")
    monkeypatch.setattr(repo_sync, "_default_base_ref", lambda p: "origin/main")

    call_log = []

    def fake_run_git(cmd, cwd, timeout=60):
        call_log.append(cmd)
        if cmd[:2] == ["git", "pull"]:
            return 1, "", "diverged"
        return 0, "", ""

    monkeypatch.setattr(repo_sync, "_run_git", fake_run_git)

    result = await repo_sync._sync_one_repo(repo_path)
    assert result["ok"] is True
    assert result["forced"] is True
    assert any(cmd[:2] == ["git", "reset"] for cmd in call_log)
    # exact fallback sequence after the failed pull: fetch, checkout -f, reset --hard
    assert call_log == [
        ["git", "fetch", "origin"],
        ["git", "checkout", "main"],
        ["git", "pull", "--ff-only", "origin", "main"],
        ["git", "fetch", "origin"],
        ["git", "checkout", "-f", "main"],
        ["git", "reset", "--hard", "origin/main"],
    ]


@pytest.mark.asyncio
async def test_sync_one_repo_falls_back_when_fetch_fails(monkeypatch, tmp_path):
    """正常路径第一步（fetch）就失败也应该走强制回退，而不是提前 return。"""
    from app.crashguard.services import repo_sync

    repo_path = str(tmp_path)
    monkeypatch.setattr(repo_sync, "_resolve_remote_name", lambda p: "origin")
    monkeypatch.setattr(repo_sync, "_default_base_ref", lambda p: "origin/main")

    call_log = []
    # first fetch fails, everything after (including forced fetch) succeeds
    responses = iter([(1, "", "network down")])

    def fake_run_git(cmd, cwd, timeout=60):
        call_log.append(cmd)
        try:
            return next(responses)
        except StopIteration:
            return 0, "", ""

    monkeypatch.setattr(repo_sync, "_run_git", fake_run_git)

    result = await repo_sync._sync_one_repo(repo_path)
    assert result["ok"] is True
    assert result["forced"] is True
    # should not have attempted checkout/pull on the normal path once fetch failed
    assert call_log[0] == ["git", "fetch", "origin"]
    assert call_log[1] == ["git", "fetch", "origin"]
    assert call_log[2] == ["git", "checkout", "-f", "main"]
    assert call_log[3] == ["git", "reset", "--hard", "origin/main"]


@pytest.mark.asyncio
async def test_sync_one_repo_forced_fetch_failure_reports_error(monkeypatch, tmp_path):
    from app.crashguard.services import repo_sync

    repo_path = str(tmp_path)
    monkeypatch.setattr(repo_sync, "_resolve_remote_name", lambda p: "origin")
    monkeypatch.setattr(repo_sync, "_default_base_ref", lambda p: "origin/main")

    def fake_run_git(cmd, cwd, timeout=60):
        return 1, "", "always fails"

    monkeypatch.setattr(repo_sync, "_run_git", fake_run_git)

    result = await repo_sync._sync_one_repo(repo_path)
    assert result["ok"] is False
    assert result["forced"] is True
    assert "forced fetch failed" in result["error"]


@pytest.mark.asyncio
async def test_sync_one_repo_missing_path_returns_error():
    from app.crashguard.services import repo_sync

    result = await repo_sync._sync_one_repo("/definitely/not/a/real/path")
    assert result["ok"] is False
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_run_repo_sync_aggregates_results(monkeypatch):
    from app.crashguard.services import repo_sync

    monkeypatch.setattr(repo_sync, "_collect_repo_paths", lambda: ["/a", "/b", "/c"])

    async def fake_sync_one_repo(path):
        return {"repo_path": path, "ok": path != "/b", "forced": False,
                "error": "" if path != "/b" else "boom"}

    monkeypatch.setattr(repo_sync, "_sync_one_repo", fake_sync_one_repo)

    res = await repo_sync.run_repo_sync()
    assert res["total"] == 3
    assert res["ok"] == 2
    assert res["failed"] == 1
    assert len(res["results"]) == 3
