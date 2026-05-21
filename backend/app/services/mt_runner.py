"""
mt-tool wrapper for multi-repo git operations in the release workspace.

`mt` fans out a single git command across all configured sub-repos (common /
global / cn). This module wraps `mt` subprocess calls with:

  - asyncio-friendly execution (run in thread pool)
  - a file lock under `$code_repo_app/.jarvis.lock` shared with repo_updater
  - structured error reporting (stderr is surfaced as MtRunnerError.message)
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("jarvis.mt_runner")

LOCK_FILENAME = ".jarvis.lock"
DEFAULT_LOCK_TIMEOUT_SEC = 60
DEFAULT_CMD_TIMEOUT_SEC = 300


class MtRunnerError(RuntimeError):
    """Non-zero exit from `mt` / `git`. Carries stderr for surface to API."""

    def __init__(self, message: str, *, stderr: str = "", returncode: int = -1):
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


@contextlib.contextmanager
def workspace_lock(workspace: Path, timeout_sec: int = DEFAULT_LOCK_TIMEOUT_SEC):
    """Cross-process exclusive lock on `$workspace/.jarvis.lock`.

    Blocks up to `timeout_sec` waiting; raises TimeoutError if not acquired.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    lock_path = workspace / LOCK_FILENAME
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        # Spin with sleep — fcntl.flock has no native timeout. We poll every
        # 0.2s up to `timeout_sec`. Sync context (called from to_thread).
        start = _monotonic()
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, IOError) as e:
                if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                if _monotonic() - start > timeout_sec:
                    raise TimeoutError(f"workspace lock not acquired within {timeout_sec}s")
                _sleep(0.2)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _monotonic() -> float:
    import time
    return time.monotonic()


def _sleep(seconds: float) -> None:
    import time
    time.sleep(seconds)


def _run(cmd: List[str], cwd: Path, timeout: int = DEFAULT_CMD_TIMEOUT_SEC) -> Tuple[str, str]:
    """Run a subprocess and return (stdout, stderr). Raises MtRunnerError on non-zero."""
    logger.debug("[mt] running: %s (cwd=%s)", " ".join(cmd), cwd)
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise MtRunnerError(
            f"command timed out after {timeout}s: {' '.join(cmd)}",
            stderr=(e.stderr or "") if isinstance(e.stderr, str) else "",
        )
    if result.returncode != 0:
        # Truncate stderr to avoid blowing up logs / HTTP responses.
        err = (result.stderr or "").strip()
        out = (result.stdout or "").strip()
        snippet = (err or out)[:2000]
        raise MtRunnerError(
            f"`{' '.join(cmd)}` failed (rc={result.returncode}): {snippet[:300]}",
            stderr=snippet,
            returncode=result.returncode,
        )
    return result.stdout, result.stderr


class MtRunner:
    """Synchronous mt operations. Wrap calls in asyncio.to_thread from async code."""

    def __init__(
        self,
        workspace: Path,
        mt_bin: str = "mt",
        exclude_subrepos: Optional[List[str]] = None,
    ):
        self.workspace = workspace
        self.mt_bin = mt_bin
        # Sub-repo names (just the directory name) to exclude from
        # release-relevant fan-outs (checkout -b / push / audit snapshot).
        self.exclude_subrepos = list(exclude_subrepos or [])

    # `--no-confirm` is mt's global flag to skip high-risk confirmation prompts.
    # Required when running mt from a non-TTY subprocess (mt v1.4+ otherwise
    # cancels with "已取消执行").
    _NC = "--no-confirm"

    def _exclude_args(self) -> List[str]:
        """Build `--exclude X --exclude Y ...` for mt commands that should
        skip tool/script sub-repos. Empty if no excludes configured."""
        out: List[str] = []
        for repo in self.exclude_subrepos:
            out += ["--exclude", repo]
        return out

    # ---- mt fan-out commands ---------------------------------------------
    def reset_workspace(self) -> None:
        """`mt reset --hard && mt fetch` — wipe tracked-file mods, refresh refs.

        We intentionally do NOT run `mt clean -fd` here: mt v1.4+ exposes its
        own `clean` *tool* command (Android/iOS/Flutter cache clean) that
        shadows the `git clean` fan-out and rejects the `-fd` flags. Untracked
        files in sub-repos don't block branch creation, so reset is enough.

        NOTE: reset applies to ALL sub-repos (no exclude) — workspace
        housekeeping should leave nothing dirty anywhere.
        """
        _run([self.mt_bin, self._NC, "reset", "--hard"], self.workspace)
        # Intentionally NO --prune — sub-repos with stale .lock files from
        # crashed prior fetches would otherwise fail the whole call.
        _run([self.mt_bin, "fetch", "--all"], self.workspace)

    def checkout_main_and_pull(self) -> None:
        """Switch every sub-repo to main and fast-forward. (all sub-repos)"""
        self.checkout_source_and_pull("main")

    def checkout_source_and_pull(self, source: str) -> None:
        """Switch every sub-repo to `source` and fast-forward.

        Used for both the canonical `main` flow and the hotfix flow where
        `source` is an existing `release/*` branch. Caller is responsible for
        verifying the branch exists on remote (see `list_remote_branches`).
        """
        _run([self.mt_bin, self._NC, "checkout", source], self.workspace)
        _run([self.mt_bin, "pull", "origin", source], self.workspace)

    def checkout_new_branch(self, branch: str) -> None:
        """`mt checkout -b <branch>` — create branch in product sub-repos only.

        Excludes tool/script sub-repos per `self.exclude_subrepos`.
        """
        args = [self.mt_bin, self._NC] + self._exclude_args() + ["checkout", "-b", branch]
        _run(args, self.workspace)

    def push_branch(self, branch: str, set_upstream: bool = True) -> None:
        """Push the release branch from product sub-repos only."""
        args = [self.mt_bin, self._NC] + self._exclude_args() + ["push"]
        if set_upstream:
            args += ["-u", "origin", branch]
        else:
            args += ["origin", branch]
        _run(args, self.workspace)

    def checkout_existing_branch(self, branch: str) -> None:
        """`mt checkout <branch>` (no -b). Used for restoring to main —
        operates on ALL sub-repos so we don't leave tool repos on a stale
        feature branch.
        """
        _run([self.mt_bin, self._NC, "checkout", branch], self.workspace)

    # ---- per-sub-repo git helpers ----------------------------------------
    def list_subrepos(self, include_excluded: bool = False) -> List[Path]:
        """Each immediate sub-dir with a .git dir is treated as a sub-repo.

        By default skips entries in `self.exclude_subrepos` so audit/snapshot
        callers only see product sub-repos. Pass `include_excluded=True` to
        get the full list (e.g. for workspace-wide reset).
        """
        subs: List[Path] = []
        for child in sorted(self.workspace.iterdir()):
            if not (child.is_dir() and (child / ".git").exists()):
                continue
            if not include_excluded and child.name in self.exclude_subrepos:
                continue
            subs.append(child)
        return subs

    def get_commits(self) -> Dict[str, str]:
        """Return {sub_repo_name: HEAD_sha} for product sub-repos only."""
        out: Dict[str, str] = {}
        for sub in self.list_subrepos():
            stdout, _ = _run(["git", "rev-parse", "HEAD"], sub, timeout=15)
            out[sub.name] = stdout.strip()
        return out

    def get_subrepo_path(self, name: str) -> Optional[Path]:
        """Return the absolute path to a sub-repo by directory name (e.g. 'common')."""
        candidate = self.workspace / name
        if candidate.is_dir() and (candidate / ".git").exists():
            return candidate
        return None

    def git(self, sub: Path, args: List[str], timeout: int = 60) -> Tuple[str, str]:
        """Run a plain `git` command inside a single sub-repo."""
        return _run(["git"] + args, sub, timeout=timeout)

    def list_remote_branches(self) -> List[str]:
        """Return the intersection of remote branch names across product sub-repos.

        A branch is "available as a source" only if every product sub-repo has
        it on origin — otherwise `mt checkout <source>` would fail partway
        through. Workspace is assumed fetched (call `reset_workspace()` first
        if you need fresh data).
        """
        per_sub: List[set] = []
        for sub in self.list_subrepos():
            stdout, _ = _run(
                ["git", "ls-remote", "--heads", "origin"],
                sub,
                timeout=30,
            )
            names: set = set()
            for line in stdout.splitlines():
                # format: "<sha>\trefs/heads/<branch>"
                parts = line.split("\t", 1)
                if len(parts) != 2:
                    continue
                ref = parts[1].strip()
                if ref.startswith("refs/heads/"):
                    names.add(ref[len("refs/heads/"):])
            per_sub.append(names)
        if not per_sub:
            return []
        common = set.intersection(*per_sub)
        return sorted(common)

    def delete_local_branch(self, branch: str) -> None:
        """Best-effort cleanup: `mt branch -D <branch>` (used to roll back failed creates)."""
        try:
            _run([self.mt_bin, self._NC, "checkout", "main"], self.workspace)
        except MtRunnerError as e:
            logger.warning("delete_local_branch: checkout main failed: %s", e)
        try:
            _run([self.mt_bin, self._NC, "branch", "-D", branch], self.workspace)
        except MtRunnerError as e:
            logger.warning("delete_local_branch: branch -D failed (ok if absent): %s", e)


# ---------------------------------------------------------------------------
# Async facade — call from API / worker code
# ---------------------------------------------------------------------------
async def run_in_lock(
    workspace: Path,
    func,
    *args,
    lock_timeout: int = DEFAULT_LOCK_TIMEOUT_SEC,
    **kwargs,
):
    """Run `func(*args, **kwargs)` in a worker thread holding the workspace lock."""

    def _wrapped():
        with workspace_lock(workspace, timeout_sec=lock_timeout):
            return func(*args, **kwargs)

    return await asyncio.to_thread(_wrapped)
