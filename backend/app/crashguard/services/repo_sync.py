"""每日仓库同步任务 —— 保证 crashguard 自动 PR 的本地 checkout 不会变旧。

只覆盖 crashguard 自己实际监控崩溃、会去开 PR 的 platform（android/ios），不管
是 flutter 世代还是 native 世代的 band 都同步。不覆盖 web/desktop/mcp——那些是
工单处理未来要支持的范围，不是 crashguard 崩溃分析/自动 PR 的范围，见
docs/superpowers/specs/2026-07-10-crashguard-4x-migration-design.md Section F。

正常路径：fetch + checkout 默认分支 + ff-only pull。
失败路径（正常路径任一步报错）：强制 fetch + checkout -f + reset --hard。

复用 pr_drafter.py 已有的 per-repo 锁 + git helper，防止和进行中的 auto-PR
git 操作打架。
"""
from __future__ import annotations

import logging
import os
from typing import Dict, List

from app.config import get_repo_routing
from app.crashguard.services.pr_drafter import (
    _acquire_repo_lock,
    _default_base_ref,
    _resolve_remote_name,
    _run_git,
)

logger = logging.getLogger("crashguard.repo_sync")

# crashguard 自己实际监控崩溃、会去开 PR 的 platform —— 不含 web/desktop/mcp
_MONITORED_PLATFORMS = ("android", "ios")


def _collect_repo_paths() -> List[str]:
    """枚举 crashguard 监控平台下所有 band 的 sub_repo_path，去重（保持首次出现顺序）。"""
    routing = get_repo_routing()
    paths: List[str] = []
    seen = set()
    for platform in _MONITORED_PLATFORMS:
        cfg = routing.get(platform) or {}
        for band in cfg.get("bands") or []:
            wrapper = os.path.expanduser(band.get("wrapper", "") or "")
            if not wrapper:
                continue
            sub = (band.get("sub", "") or "").strip()
            path = os.path.join(wrapper, sub) if sub else wrapper
            if path in seen:
                continue
            seen.add(path)
            paths.append(path)
    return paths


def _branch_from_base_ref(base_ref: str, remote: str) -> str:
    """'origin/main' + remote='origin' -> 'main'；解析失败兜底 'main'。"""
    prefix = f"{remote}/"
    if base_ref.startswith(prefix):
        return base_ref[len(prefix):]
    return "main"


async def _sync_one_repo(repo_path: str) -> Dict:
    """同步单仓：正常路径 fetch+checkout+ff-only pull；失败则强制 fetch+reset --hard。"""
    if not os.path.isdir(repo_path):
        return {"repo_path": repo_path, "ok": False, "forced": False, "error": "path not found"}

    lock = await _acquire_repo_lock(repo_path)
    async with lock:
        remote = _resolve_remote_name(repo_path)
        base_ref = _default_base_ref(repo_path)
        branch = _branch_from_base_ref(base_ref, remote)

        rc, _, err = _run_git(["git", "fetch", remote], repo_path, timeout=120)
        if rc == 0:
            rc2, _, err2 = _run_git(["git", "checkout", branch], repo_path, timeout=30)
            if rc2 == 0:
                rc3, _, err3 = _run_git(
                    ["git", "pull", "--ff-only", remote, branch], repo_path, timeout=60,
                )
                if rc3 == 0:
                    return {"repo_path": repo_path, "ok": True, "forced": False, "error": ""}
                err = err3
            else:
                err = err2
        logger.warning("repo_sync: normal path failed for %s (%s), forcing sync", repo_path, err)

        rc, _, ferr = _run_git(["git", "fetch", remote], repo_path, timeout=120)
        if rc != 0:
            return {"repo_path": repo_path, "ok": False, "forced": True, "error": f"forced fetch failed: {ferr}"}
        rc, _, ferr = _run_git(["git", "checkout", "-f", branch], repo_path, timeout=30)
        if rc != 0:
            return {"repo_path": repo_path, "ok": False, "forced": True, "error": f"forced checkout failed: {ferr}"}
        rc, _, ferr = _run_git(
            ["git", "reset", "--hard", f"{remote}/{branch}"], repo_path, timeout=30,
        )
        if rc != 0:
            return {"repo_path": repo_path, "ok": False, "forced": True, "error": f"forced reset failed: {ferr}"}
        return {"repo_path": repo_path, "ok": True, "forced": True, "error": ""}


async def run_repo_sync() -> Dict:
    """主入口：同步所有 crashguard 监控平台的仓库 checkout。"""
    paths = _collect_repo_paths()
    results = [await _sync_one_repo(p) for p in paths]
    for r in results:
        if r["ok"]:
            logger.info("repo_sync: %s ok (forced=%s)", r["repo_path"], r["forced"])
        else:
            logger.warning("repo_sync: %s FAILED: %s", r["repo_path"], r["error"])
    ok_count = sum(1 for r in results if r["ok"])
    return {
        "total": len(results),
        "ok": ok_count,
        "failed": len(results) - ok_count,
        "results": results,
    }
