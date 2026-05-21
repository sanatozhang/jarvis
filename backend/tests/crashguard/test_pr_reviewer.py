"""PR Reviewer 单元测试 — Task 2-7"""
from collections import Counter
from unittest.mock import MagicMock, patch

import pytest

# ---------- Task 2: diff & blame 解析 ----------

def test_parse_diff_target_lines_single_file():
    from app.crashguard.services.pr_reviewer import parse_diff_target_lines
    diff = """diff --git a/lib/foo.dart b/lib/foo.dart
index 1234567..89abcde 100644
--- a/lib/foo.dart
+++ b/lib/foo.dart
@@ -10,3 +10,4 @@ class Foo {
   void bar() {
-    print("old");
+    print("new");
+    log.info("added");
   }
"""
    result = parse_diff_target_lines(diff)
    # @@ -10,3 starts old line at 10. " ctx" line 10. "-" line 11. " ctx" line 12.
    # We blame the - line (11) since that line has prior author.
    assert result == {"lib/foo.dart": [11]}


def test_parse_diff_target_lines_multifile():
    from app.crashguard.services.pr_reviewer import parse_diff_target_lines
    diff = """diff --git a/a.dart b/a.dart
--- a/a.dart
+++ b/a.dart
@@ -5,1 +5,1 @@
-old line
+new line
diff --git a/b.dart b/b.dart
--- a/b.dart
+++ b/b.dart
@@ -100,2 +100,2 @@
 ctx
-old
+new
"""
    result = parse_diff_target_lines(diff)
    assert result == {"a.dart": [5], "b.dart": [101]}


def test_parse_diff_target_lines_pure_addition_ignored():
    from app.crashguard.services.pr_reviewer import parse_diff_target_lines
    diff = """--- a/c.dart
+++ b/c.dart
@@ -10,1 +10,3 @@
 ctx
+new1
+new2
"""
    # Only context line, no - lines → no blame target
    result = parse_diff_target_lines(diff)
    assert result == {}


def test_parse_blame_author_email_porcelain():
    from app.crashguard.services.pr_reviewer import parse_blame_author_email
    porcelain = (
        "abc123def 1 1 1\n"
        "author Alice Wang\n"
        "author-mail <alice@plaud.ai>\n"
        "author-time 1700000000\n"
        "summary do something\n"
        "\tcode here\n"
    )
    assert parse_blame_author_email(porcelain) == "alice@plaud.ai"


def test_parse_blame_author_email_missing_returns_empty():
    from app.crashguard.services.pr_reviewer import parse_blame_author_email
    assert parse_blame_author_email("") == ""
    assert parse_blame_author_email("no email here\nauthor Foo") == ""


# ---------- Task 3: gh pr diff + blame 聚合 ----------

def _fake_run(stdout="", returncode=0, stderr=""):
    cp = MagicMock()
    cp.stdout = stdout
    cp.returncode = returncode
    cp.stderr = stderr
    return cp


def test_fetch_pr_diff_via_gh_success():
    from app.crashguard.services.pr_reviewer import fetch_pr_diff_via_gh
    diff = "diff --git a/a b/a\n--- a/a\n+++ b/a\n"
    with patch("subprocess.run", return_value=_fake_run(stdout=diff)):
        assert fetch_pr_diff_via_gh("https://github.com/x/y/pull/1") == diff


def test_fetch_pr_diff_via_gh_failure_returns_empty():
    from app.crashguard.services.pr_reviewer import fetch_pr_diff_via_gh
    with patch("subprocess.run", return_value=_fake_run(returncode=1)):
        assert fetch_pr_diff_via_gh("https://github.com/x/y/pull/1") == ""


def test_fetch_pr_diff_empty_url_returns_empty():
    from app.crashguard.services.pr_reviewer import fetch_pr_diff_via_gh
    assert fetch_pr_diff_via_gh("") == ""


def test_filter_authors_basic_top2_with_pct():
    from app.crashguard.services.pr_reviewer import _filter_authors
    counter = Counter({
        "alice@plaud.ai": 5,
        "jarvis-bot@plaud.ai": 10,
        "bob@plaud.ai": 2,
        "sanato.zhang@plaud.ai": 3,
    })
    blocked = ["jarvis-bot@plaud.ai", "sanato.zhang@plaud.ai"]
    out = _filter_authors(counter, blocked, top_n=2, min_lines_pct=0.20)
    # after filter: alice=5, bob=2; total=7; alice=71%, bob=28%, both ≥ 20%
    assert out == [("alice@plaud.ai", 5), ("bob@plaud.ai", 2)]


def test_filter_authors_min_pct_excludes_noise():
    from app.crashguard.services.pr_reviewer import _filter_authors
    counter = Counter({"alice@plaud.ai": 50, "bob@plaud.ai": 1})
    # total=51; bob=2% < 20% → excluded
    out = _filter_authors(counter, [], top_n=2, min_lines_pct=0.20)
    assert out == [("alice@plaud.ai", 50)]


def test_filter_authors_all_blocked_returns_empty():
    from app.crashguard.services.pr_reviewer import _filter_authors
    counter = Counter({"jarvis-bot@plaud.ai": 10})
    out = _filter_authors(counter, ["jarvis-bot@plaud.ai"], top_n=2, min_lines_pct=0.20)
    assert out == []


def test_filter_authors_case_insensitive_block():
    from app.crashguard.services.pr_reviewer import _filter_authors
    counter = Counter({"Alice@Plaud.AI": 5})
    out = _filter_authors(counter, ["alice@plaud.ai"], top_n=2, min_lines_pct=0.20)
    assert out == []


def test_resolve_reviewers_by_blame_pr_url_missing():
    from app.crashguard.services.pr_reviewer import resolve_reviewers_by_blame
    settings = MagicMock()
    r = resolve_reviewers_by_blame("", "/tmp/repo", settings)
    assert r.reason == "pr_url_missing"


def test_resolve_reviewers_by_blame_diff_empty():
    from app.crashguard.services.pr_reviewer import resolve_reviewers_by_blame
    settings = MagicMock()
    with patch("subprocess.run", return_value=_fake_run(stdout="", returncode=0)):
        r = resolve_reviewers_by_blame("https://github.com/x/y/pull/1",
                                        "/tmp/repo", settings)
    assert r.reason == "diff_empty"


def test_resolve_reviewers_by_blame_repo_missing(tmp_path):
    from app.crashguard.services.pr_reviewer import resolve_reviewers_by_blame
    settings = MagicMock()
    diff = ("--- a/x.dart\n+++ b/x.dart\n@@ -1,1 +1,1 @@\n-old\n+new\n")
    nonexistent = str(tmp_path / "nope")
    with patch("subprocess.run", return_value=_fake_run(stdout=diff)):
        r = resolve_reviewers_by_blame("https://github.com/x/y/pull/1",
                                        nonexistent, settings)
    assert r.reason == "repo_missing"


def test_resolve_reviewers_by_blame_bot_only(tmp_path):
    """blame 出来全是 blocked author 时 reason=bot_only"""
    from app.crashguard.services.pr_reviewer import resolve_reviewers_by_blame
    settings = MagicMock()
    settings.pr_reviewer_blocked_authors = ["jarvis-bot@plaud.ai"]
    settings.pr_reviewer_top_n = 2
    settings.pr_reviewer_min_lines_pct = 0.20

    diff = "--- a/x.dart\n+++ b/x.dart\n@@ -1,1 +1,1 @@\n-old\n+new\n"
    porcelain = (
        "abc 1 1 1\nauthor Bot\nauthor-mail <jarvis-bot@plaud.ai>\n"
        "summary x\n\tcode\n"
    )

    def fake_run(cmd, **kw):
        if "gh" in cmd[0]:
            return _fake_run(stdout=diff)
        if "blame" in cmd:
            return _fake_run(stdout=porcelain)
        return _fake_run(returncode=1)

    with patch("subprocess.run", side_effect=fake_run):
        r = resolve_reviewers_by_blame("https://github.com/x/y/pull/1",
                                        str(tmp_path), settings)
    assert r.reason == "bot_only"


def test_resolve_reviewers_by_blame_happy_path(tmp_path):
    from app.crashguard.services.pr_reviewer import resolve_reviewers_by_blame
    settings = MagicMock()
    settings.pr_reviewer_blocked_authors = []
    settings.pr_reviewer_top_n = 2
    settings.pr_reviewer_min_lines_pct = 0.20

    diff = ("--- a/x.dart\n+++ b/x.dart\n@@ -1,3 +1,3 @@\n-l1\n-l2\n-l3\n+n1\n+n2\n+n3\n")
    porcelain_alice = (
        "abc 1 1 1\nauthor Alice\nauthor-mail <alice@plaud.ai>\n"
        "summary x\n\tcode\n"
    )

    def fake_run(cmd, **kw):
        if cmd[0] == "gh":
            return _fake_run(stdout=diff)
        if "blame" in cmd:
            return _fake_run(stdout=porcelain_alice)
        return _fake_run(returncode=1)

    with patch("subprocess.run", side_effect=fake_run):
        r = resolve_reviewers_by_blame("https://github.com/x/y/pull/1",
                                        str(tmp_path), settings)
    assert r.reason == "ok"
    assert r.emails == ["alice@plaud.ai"]
    assert r.line_counts == {"alice@plaud.ai": 3}
