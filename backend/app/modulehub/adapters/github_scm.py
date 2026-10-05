"""ScmHost port on the GitHub REST API (+ the git CLI for backports).

The token comes from `MODULEHUB_GITHUB_TOKEN` or, when that is empty, the server's `gh` CLI login
(`resolve_token`). It is never logged; git output is scrubbed before it is raised.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import shutil
import subprocess
import tempfile
from typing import Awaitable, Callable, List, Optional, Tuple

import httpx

from app.modulehub.ports import PullRequest

RunGit = Callable[[List[str], str], Awaitable[Tuple[int, str]]]

logger = logging.getLogger("jarvis.modulehub")


def resolve_token(explicit: str = "") -> str:
    """Explicit setting first, else `gh auth token` (the OAuth login crashguard already uses on the server).

    GH_TOKEN / GITHUB_TOKEN are deliberately NOT a fallback, and are stripped from the `gh` subprocess env:
    personal PATs outlive the Plaud-AI org's 90-day policy and get hard-rejected (403), and `gh` itself
    would prefer such an env PAT over its own OAuth login. Same policy as crashguard's github_symbols /
    pr_drafter. Returns "" when nothing is available (callers surface that as a clear ScmError).
    """
    if explicit:
        return explicit
    env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        logger.error("modulehub: no MODULEHUB_GITHUB_TOKEN and `gh auth token` failed: %s", e)
        return ""
    token = (r.stdout or "").strip() if r.returncode == 0 else ""
    if not token:
        logger.error("modulehub: no MODULEHUB_GITHUB_TOKEN and `gh` is not logged in; GitHub calls will fail")
    return token


class ScmError(RuntimeError):
    pass


async def _default_run_git(args: List[str], cwd: str) -> Tuple[int, str]:
    proc = await asyncio.create_subprocess_exec("git", *args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace")


class GitHubScm:
    def __init__(self, token: str, *, client: Optional[httpx.AsyncClient] = None, run_git: Optional[RunGit] = None,
                 api_base: str = "https://api.github.com", clone_base: str = "https://github.com"):
        self._token = token
        self._client = client or httpx.AsyncClient(base_url=api_base, timeout=30.0)
        self._run_git = run_git or _default_run_git
        self._clone_base = clone_base

    # ---- plumbing ----------------------------------------------------------------------------
    def _h(self):
        if not self._token:
            # an empty token would otherwise surface as httpx "Illegal header value b'Bearer '"
            raise ScmError("no GitHub token: set MODULEHUB_GITHUB_TOKEN or log in with `gh auth login`")
        return {"Authorization": "Bearer %s" % self._token, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

    async def _req(self, method: str, path: str, ok=(200, 201), **kw) -> httpx.Response:
        r = await self._client.request(method, path, headers=self._h(), **kw)
        if r.status_code not in ok:
            raise ScmError("%s %s -> %s %s" % (method, path, r.status_code, r.text[:200]))
        return r

    async def _sha(self, repo: str, ref: str) -> str:
        return (await self._req("GET", "/repos/%s/commits/%s" % (repo, ref))).json()["sha"]

    # ---- reads -------------------------------------------------------------------------------
    async def read_file(self, repo: str, ref: str, path: str) -> Optional[str]:
        r = await self._req("GET", "/repos/%s/contents/%s" % (repo, path), ok=(200, 404), params={"ref": ref})
        if r.status_code == 404:
            return None
        return base64.b64decode(r.json()["content"]).decode("utf-8")

    async def list_branches(self, repo: str, prefix: str) -> List[str]:
        r = await self._req("GET", "/repos/%s/git/matching-refs/heads/%s" % (repo, prefix), params={"per_page": 100})
        return [x["ref"][len("refs/heads/"):] for x in r.json()]

    async def branch_contains(self, repo: str, branch: str, ref: str) -> bool:
        r = await self._req("GET", "/repos/%s/compare/%s...%s" % (repo, ref, branch))
        return r.json()["status"] in ("ahead", "identical")

    async def commits_between(self, repo: str, base_ref: str, head_ref: str) -> List[str]:
        r = await self._req("GET", "/repos/%s/compare/%s...%s" % (repo, base_ref, head_ref))
        return [c["commit"]["message"].splitlines()[0] for c in r.json().get("commits", [])]

    async def changed_files(self, repo: str, base_ref: str, head_ref: str) -> List[str]:
        """Paths changed between two refs (GitHub lists at most 300; enough for the mirror's per-directory check)."""
        r = await self._req("GET", "/repos/%s/compare/%s...%s" % (repo, base_ref, head_ref))
        return [f["filename"] for f in r.json().get("files", []) or []]

    async def range_applied_on(self, repo: str, base: str, commit_range: str) -> bool:
        """True when the range's head tag is already an ancestor of `base` (merged, not merely cherry-picked)."""
        head = commit_range.split("..")[-1]
        r = await self._req("GET", "/repos/%s/compare/%s...%s" % (repo, head, base), ok=(200, 404))
        return r.status_code == 200 and r.json()["status"] in ("ahead", "identical")

    # ---- writes ------------------------------------------------------------------------------
    async def create_branch(self, repo: str, name: str, from_ref: str) -> None:
        sha = await self._sha(repo, from_ref)
        await self._req("POST", "/repos/%s/git/refs" % repo, ok=(201, 422), json={"ref": "refs/heads/%s" % name, "sha": sha})

    async def _find_open_pr(self, repo: str, branch: str) -> Optional[dict]:
        owner = repo.split("/")[0]
        r = await self._req("GET", "/repos/%s/pulls" % repo, params={"head": "%s:%s" % (owner, branch), "state": "open"})
        found = r.json()
        return found[0] if found else None

    async def open_or_update_file_pr(self, *, repo: str, base: str, branch: str, path: str, content: str,
                                     title: str, body: str, commit_message: str) -> PullRequest:
        base_sha = await self._sha(repo, base)
        exists = (await self._req("GET", "/repos/%s/git/ref/heads/%s" % (repo, branch), ok=(200, 404))).status_code == 200
        if exists:  # the bump PR only ever carries this one change, so resetting it to the base is lossless
            await self._req("PATCH", "/repos/%s/git/refs/heads/%s" % (repo, branch), json={"sha": base_sha, "force": True})
        else:
            await self._req("POST", "/repos/%s/git/refs" % repo, json={"ref": "refs/heads/%s" % branch, "sha": base_sha})
        current = (await self._req("GET", "/repos/%s/contents/%s" % (repo, path), params={"ref": branch})).json()
        await self._req("PUT", "/repos/%s/contents/%s" % (repo, path), json={
            "message": commit_message, "branch": branch, "sha": current["sha"],
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii")})
        pr = await self._find_open_pr(repo, branch)
        if pr:
            await self._req("PATCH", "/repos/%s/pulls/%d" % (repo, pr["number"]), json={"title": title, "body": body})
            return PullRequest(number=pr["number"], url=pr["html_url"], created=False)
        made = (await self._req("POST", "/repos/%s/pulls" % repo, json={"title": title, "head": branch, "base": base, "body": body})).json()
        return PullRequest(number=made["number"], url=made["html_url"])

    async def open_backport_pr(self, *, repo: str, branch: str, base: str, commit_range: str, title: str, body: str) -> PullRequest:
        pr = await self._find_open_pr(repo, branch)
        if pr:
            return PullRequest(number=pr["number"], url=pr["html_url"], created=False)
        conflict = await self._push_backport_branch(repo, branch, base, commit_range)
        made = (await self._req("POST", "/repos/%s/pulls" % repo, json={
            "title": ("[CONFLICT] " if conflict else "") + title, "head": branch, "base": base, "body": body})).json()
        return PullRequest(number=made["number"], url=made["html_url"], conflict=conflict)

    async def _push_backport_branch(self, repo: str, branch: str, base: str, commit_range: str) -> bool:
        work = tempfile.mkdtemp(prefix="modulehub-backport-")
        url = "%s/%s.git" % (self._clone_base.replace("https://", "https://x-access-token:%s@" % self._token), repo)

        async def git(*args: str, check: bool = True) -> Tuple[int, str]:
            rc, out = await self._run_git(list(args), work)
            if check and rc != 0:
                raise ScmError("git %s failed: %s" % (args[0], out.replace(self._token, "***")[:300]))
            return rc, out

        try:
            await git("clone", "--quiet", "--no-single-branch", url, ".")
            await git("config", "user.name", "jarvis-modulehub")
            await git("config", "user.email", "modulehub@plaud.ai")
            await git("fetch", "--quiet", "--tags")
            await git("checkout", "--quiet", "-b", branch, "origin/%s" % base)
            rc, _ = await git("cherry-pick", commit_range, check=False)
            conflict = rc != 0
            if conflict:
                await git("cherry-pick", "--abort", check=False)
                await git("commit", "--allow-empty", "--quiet", "-m", "chore: backport placeholder (cherry-pick conflicts, resolve manually)")
            await git("push", "--quiet", "origin", branch)
            return conflict
        finally:
            shutil.rmtree(work, ignore_errors=True)
