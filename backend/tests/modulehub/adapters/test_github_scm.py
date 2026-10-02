import base64
import json

import httpx
import pytest

from app.modulehub.adapters.github_scm import GitHubScm, ScmError

REPO = "Plaud-AI/shell"


def make(routes, run_git=None):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, dict(req.url.params), body))
        key = (req.method, req.url.path)
        res = routes.get(key)
        if callable(res):
            res = res(req, body)
        if res is None:
            return httpx.Response(404, json={"message": "nf"})
        status, data = res
        return httpx.Response(status, json=data)

    client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    return GitHubScm("SECRET", client=client, run_git=run_git), calls


async def test_read_file_and_missing():
    content = base64.b64encode(b"hello").decode()
    scm, calls = make({("GET", "/repos/%s/contents/a.toml" % REPO): (200, {"content": content})})
    assert await scm.read_file(REPO, "main", "a.toml") == "hello"
    assert calls[0][2] == {"ref": "main"}
    assert await scm.read_file(REPO, "main", "missing") is None


async def test_list_branches_strips_prefix_and_sends_auth():
    scm, calls = make({("GET", "/repos/%s/git/matching-refs/heads/release/" % REPO): (200, [{"ref": "refs/heads/release/1"}, {"ref": "refs/heads/release/2"}])})
    assert await scm.list_branches(REPO, "release/") == ["release/1", "release/2"]


async def test_errors_raise_scm_error_without_leaking_token():
    scm, _ = make({("GET", "/repos/%s/commits/x" % REPO): (500, {"message": "boom"})})
    with pytest.raises(ScmError) as e:
        await scm._sha(REPO, "x")
    assert "SECRET" not in str(e.value)


async def test_create_branch_tolerates_existing():
    scm, calls = make({("GET", "/repos/%s/commits/v1.0.0" % REPO): (200, {"sha": "abc"}),
                       ("POST", "/repos/%s/git/refs" % REPO): (422, {"message": "Reference already exists"})})
    await scm.create_branch(REPO, "release/1", "v1.0.0")
    assert calls[-1][3] == {"ref": "refs/heads/release/1", "sha": "abc"}


@pytest.mark.parametrize("status,expected", [("ahead", True), ("identical", True), ("behind", False), ("diverged", False)])
async def test_branch_contains(status, expected):
    scm, _ = make({("GET", "/repos/%s/compare/v1.0.0...release/1" % REPO): (200, {"status": status})})
    assert await scm.branch_contains(REPO, "release/1", "v1.0.0") is expected


async def test_changed_files_lists_compare_files():
    scm, _ = make({("GET", "/repos/%s/compare/v1...v2" % REPO): (200, {"files": [{"filename": "ios/A.swift"}, {"filename": "android/b.kt"}]})})
    assert await scm.changed_files(REPO, "v1", "v2") == ["ios/A.swift", "android/b.kt"]


async def test_commits_between_uses_first_line():
    scm, _ = make({("GET", "/repos/%s/compare/v1...v2" % REPO): (200, {"commits": [{"commit": {"message": "feat: a\n\nbody"}}]})})
    assert await scm.commits_between(REPO, "v1", "v2") == ["feat: a"]


async def test_range_applied_on():
    scm, _ = make({("GET", "/repos/%s/compare/v1.0.1...main" % REPO): (200, {"status": "ahead"})})
    assert await scm.range_applied_on(REPO, "main", "v1.0.0..v1.0.1") is True
    scm, _ = make({})
    assert await scm.range_applied_on(REPO, "main", "v1.0.0..v1.0.1") is False


def _bump_routes(ref_exists, pr_exists):
    r = {
        ("GET", "/repos/%s/commits/main" % REPO): (200, {"sha": "base"}),
        ("GET", "/repos/%s/git/ref/heads/bump" % REPO): (200, {}) if ref_exists else (404, {}),
        ("PATCH", "/repos/%s/git/refs/heads/bump" % REPO): (200, {}),
        ("POST", "/repos/%s/git/refs" % REPO): (201, {}),
        ("GET", "/repos/%s/contents/m.toml" % REPO): (200, {"sha": "filesha", "content": ""}),
        ("PUT", "/repos/%s/contents/m.toml" % REPO): (200, {}),
        ("GET", "/repos/%s/pulls" % REPO): (200, [{"number": 5, "html_url": "https://gh/5"}] if pr_exists else []),
        ("PATCH", "/repos/%s/pulls/5" % REPO): (200, {}),
        ("POST", "/repos/%s/pulls" % REPO): (201, {"number": 6, "html_url": "https://gh/6"}),
    }
    return r


async def test_bump_pr_new_branch_new_pr():
    scm, calls = make(_bump_routes(False, False))
    pr = await scm.open_or_update_file_pr(repo=REPO, base="main", branch="bump", path="m.toml", content="x",
                                          title="t", body="b", commit_message="c")
    assert (pr.number, pr.created) == (6, True)
    assert ("POST", "/repos/%s/git/refs" % REPO) in [(c[0], c[1]) for c in calls]
    put = [c for c in calls if c[0] == "PUT"][0][3]
    assert put["branch"] == "bump" and put["sha"] == "filesha" and base64.b64decode(put["content"]) == b"x"


async def test_bump_pr_existing_branch_and_pr_are_reused():
    scm, calls = make(_bump_routes(True, True))
    pr = await scm.open_or_update_file_pr(repo=REPO, base="main", branch="bump", path="m.toml", content="x",
                                          title="t", body="b", commit_message="c")
    assert (pr.number, pr.created) == (5, False)
    patch = [c for c in calls if c[0] == "PATCH" and c[1].endswith("refs/heads/bump")][0][3]
    assert patch == {"sha": "base", "force": True}
    assert not [c for c in calls if c[0] == "POST" and c[1].endswith("/pulls")]


class GitLog:
    def __init__(self, fail=()):
        self.cmds, self.fail = [], set(fail)

    async def __call__(self, args, cwd):
        self.cmds.append(args)
        return (1, "boom SECRET") if args[0] in self.fail else (0, "")


def _backport_routes(pr_exists=False):
    return {("GET", "/repos/%s/pulls" % REPO): (200, [{"number": 9, "html_url": "https://gh/9"}] if pr_exists else []),
            ("POST", "/repos/%s/pulls" % REPO): (201, {"number": 10, "html_url": "https://gh/10"})}


async def test_backport_clean_cherry_pick():
    git = GitLog()
    scm, calls = make(_backport_routes(), run_git=git)
    pr = await scm.open_backport_pr(repo=REPO, branch="bp", base="main", commit_range="v1..v2", title="t", body="b")
    assert not pr.conflict and pr.number == 10
    assert ["cherry-pick", "v1..v2"] in git.cmds and ["push", "--quiet", "origin", "bp"] in git.cmds
    assert not any(c[0] == "cherry-pick" and c[1] == "--abort" for c in [(x[0], x[1]) for x in git.cmds if len(x) > 1])
    assert [c for c in calls if c[0] == "POST"][0][3]["title"] == "t"


async def test_backport_conflict_opens_placeholder_pr():
    git = GitLog(fail=["cherry-pick"])
    scm, calls = make(_backport_routes(), run_git=git)
    pr = await scm.open_backport_pr(repo=REPO, branch="bp", base="main", commit_range="v1..v2", title="t", body="b")
    assert pr.conflict
    assert ["cherry-pick", "--abort"] in git.cmds
    assert any(c[0] == "commit" and "--allow-empty" in c for c in git.cmds)
    assert [c for c in calls if c[0] == "POST"][0][3]["title"].startswith("[CONFLICT] ")


async def test_backport_existing_pr_is_reused():
    scm, _ = make(_backport_routes(pr_exists=True), run_git=GitLog())
    pr = await scm.open_backport_pr(repo=REPO, branch="bp", base="main", commit_range="v1..v2", title="t", body="b")
    assert (pr.number, pr.created) == (9, False)


async def test_git_failure_is_scrubbed():
    scm, _ = make(_backport_routes(), run_git=GitLog(fail=["clone"]))
    with pytest.raises(ScmError) as e:
        await scm.open_backport_pr(repo=REPO, branch="bp", base="main", commit_range="v1..v2", title="t", body="b")
    assert "SECRET" not in str(e.value) and "***" in str(e.value)


async def test_default_run_git_executes_real_git(tmp_path):
    from app.modulehub.adapters.github_scm import _default_run_git

    rc, out = await _default_run_git(["--version"], str(tmp_path))
    assert rc == 0 and out.startswith("git version")
