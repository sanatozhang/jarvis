"""PR Reviewer 单元测试 — Task 2-7"""
import json
from collections import Counter
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker


@pytest.fixture
async def patched_session(db_engine):
    """复用 conftest db_engine，把全局 _session_factory 指过来。"""
    import app.db.database as db_mod
    import app.crashguard.models  # noqa: F401 — 注册 crash_* 表

    async with db_engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.create_all)

    original = db_mod._session_factory
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    db_mod._session_factory = factory
    yield factory
    db_mod._session_factory = original

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


def test_filter_authors_soft_min_pct_backfills_to_top_n():
    """软门控：占比不足但 top_n 不足时仍补足"""
    from app.crashguard.services.pr_reviewer import _filter_authors
    counter = Counter({"alice@plaud.ai": 50, "bob@plaud.ai": 1})
    # top_n=2: bob 2% < 20% 落选；但 primary=[alice] 不足 2 人 → 补 bob
    out = _filter_authors(counter, [], top_n=2, min_lines_pct=0.20)
    assert out == [("alice@plaud.ai", 50), ("bob@plaud.ai", 1)]


def test_filter_authors_hard_pct_when_enough_primary():
    """已凑齐 top_n 时不再回填"""
    from app.crashguard.services.pr_reviewer import _filter_authors
    counter = Counter({"alice@plaud.ai": 50, "bob@plaud.ai": 1})
    # top_n=1: primary=[alice] 已够，不补 bob
    out = _filter_authors(counter, [], top_n=1, min_lines_pct=0.20)
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


def test_filter_authors_domain_whitelist_strips_non_plaud_ai():
    """白名单：只 plaud.ai 域名通过，qq.com / kaaaaai.cn 直接剔除。

    抓手：治理 #1077 root@kaaaaai.cn / #1074 727732656@qq.com 等历史 commit
    被选成 reviewer 但飞书发不出去的问题。
    """
    from app.crashguard.services.pr_reviewer import _filter_authors
    counter = Counter({
        "alice@plaud.ai": 5,
        "727732656@qq.com": 8,
        "root@kaaaaai.cn": 3,
        "bob@plaud.ai": 2,
    })
    out = _filter_authors(
        counter, blocked=[], top_n=2, min_lines_pct=0.20,
        allowed_domains=["plaud.ai"],
    )
    assert out == [("alice@plaud.ai", 5), ("bob@plaud.ai", 2)]


def test_filter_authors_domain_whitelist_case_insensitive():
    """域名比较应大小写不敏感且支持前缀 @"""
    from app.crashguard.services.pr_reviewer import _filter_authors
    counter = Counter({"Alice@Plaud.AI": 5, "spam@QQ.com": 3})
    out = _filter_authors(
        counter, blocked=[], top_n=2, min_lines_pct=0.20,
        allowed_domains=["@plaud.ai"],  # 容忍前导 @
    )
    assert out == [("Alice@Plaud.AI", 5)]


def test_filter_authors_empty_allowed_domains_means_no_restriction():
    """allowed_domains=None / [] 时向后兼容旧行为，不做域名过滤"""
    from app.crashguard.services.pr_reviewer import _filter_authors
    counter = Counter({"alice@plaud.ai": 5, "bot@qq.com": 3})
    out_none = _filter_authors(counter, blocked=[], top_n=2, min_lines_pct=0.20)
    out_empty = _filter_authors(
        counter, blocked=[], top_n=2, min_lines_pct=0.20, allowed_domains=[],
    )
    assert out_none == [("alice@plaud.ai", 5), ("bot@qq.com", 3)]
    assert out_empty == out_none


def test_filter_authors_domain_then_block_combined():
    """白名单先剔除非 plaud.ai，黑名单再剔除 crashguard-bot"""
    from app.crashguard.services.pr_reviewer import _filter_authors
    counter = Counter({
        "crashguard-bot@plaud.ai": 20,
        "alice@plaud.ai": 5,
        "spam@qq.com": 100,
    })
    out = _filter_authors(
        counter,
        blocked=["crashguard-bot@plaud.ai"],
        top_n=2, min_lines_pct=0.20,
        allowed_domains=["plaud.ai"],
    )
    assert out == [("alice@plaud.ai", 5)]


def test_filter_authors_domain_whitelist_all_filtered_returns_empty():
    """白名单把所有 author 都过滤光时返回空（外层会用 bot_only 兜底）"""
    from app.crashguard.services.pr_reviewer import _filter_authors
    counter = Counter({"a@qq.com": 10, "root@kaaaaai.cn": 5})
    out = _filter_authors(
        counter, blocked=[], top_n=2, min_lines_pct=0.20,
        allowed_domains=["plaud.ai"],
    )
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


# ---------- Task 4: 飞书卡片 + 通知 ----------

def test_build_reviewer_card_contains_pr_link_and_lines():
    from app.crashguard.services.pr_reviewer import build_reviewer_card
    card = build_reviewer_card(
        pr_url="https://github.com/x/y/pull/42",
        pr_title="[crashguard][DRAFT] Fix LateInit",
        crash_title="LateInitializationError",
        crash_url="https://app.datadoghq.com/error-tracking/issue/abc",
        line_count=15,
        total_lines=20,
    )
    payload = json.dumps(card, ensure_ascii=False)
    assert "https://github.com/x/y/pull/42" in payload
    assert "15" in payload      # line_count
    assert "75" in payload      # 15/20 = 75%
    assert "请你 review" in payload


def test_build_fallback_card_contains_reason_and_unresolved():
    from app.crashguard.services.pr_reviewer import build_fallback_card
    card = build_fallback_card(
        pr_url="https://github.com/x/y/pull/42",
        pr_title="[crashguard][DRAFT] Fix LateInit",
        reason="all_unresolved",
        unresolved_emails=["alice@plaud.ai", "bob@plaud.ai"],
    )
    payload = json.dumps(card, ensure_ascii=False)
    assert "需手动指派" in payload
    assert "alice@plaud.ai" in payload
    assert "bob@plaud.ai" in payload


def test_build_fallback_card_translates_reason():
    from app.crashguard.services.pr_reviewer import build_fallback_card
    card = build_fallback_card(
        pr_url="https://github.com/x/y/pull/42",
        pr_title="[crashguard][DRAFT] X",
        reason="bot_only",
    )
    payload = json.dumps(card, ensure_ascii=False)
    assert "全部为 bot author" in payload


@pytest.mark.asyncio
async def test_notify_reviewers_ok_sends_to_each_email():
    from app.crashguard.services import pr_reviewer
    from app.crashguard.services.pr_reviewer import (
        ReviewerResolution, notify_reviewers,
    )
    pr = MagicMock()
    pr.pr_url = "https://github.com/x/y/pull/42"
    pr.pr_number = 42
    pr.repo = "plaud-flutter-global"
    pr.datadog_issue_id = "abc"

    settings = MagicMock()
    settings.pr_reviewer_fallback_email = "sanato.zhang@plaud.ai"

    res = ReviewerResolution(
        emails=["alice@plaud.ai", "bob@plaud.ai"],
        line_counts={"alice@plaud.ai": 5, "bob@plaud.ai": 3},
        reason="ok",
    )

    sent_log = []

    async def fake_send(chat_id="", card=None, email=""):
        sent_log.append(email)
        return True

    with patch("app.services.feishu_cli.send_interactive_card", side_effect=fake_send):
        sent, fb = await notify_reviewers(pr, res, settings)

    assert set(sent) == {"alice@plaud.ai", "bob@plaud.ai"}
    assert fb == ""
    assert set(sent_log) == {"alice@plaud.ai", "bob@plaud.ai"}


@pytest.mark.asyncio
async def test_notify_reviewers_send_fails_fallbacks_to_sanato():
    from app.crashguard.services.pr_reviewer import (
        ReviewerResolution, notify_reviewers,
    )
    pr = MagicMock()
    pr.pr_url = "https://github.com/x/y/pull/42"
    pr.pr_number = 42
    pr.repo = "plaud-flutter-global"
    pr.datadog_issue_id = "abc"

    settings = MagicMock()
    settings.pr_reviewer_fallback_email = "sanato.zhang@plaud.ai"

    res = ReviewerResolution(
        emails=["alice@plaud.ai"],
        line_counts={"alice@plaud.ai": 5},
        reason="ok",
    )

    sent_log = []

    async def fake_send(chat_id="", card=None, email=""):
        sent_log.append(email)
        # alice 发送失败，sanato 成功
        return email != "alice@plaud.ai"

    with patch("app.services.feishu_cli.send_interactive_card", side_effect=fake_send):
        sent, fb = await notify_reviewers(pr, res, settings)

    assert sent == []  # alice 失败
    assert fb == "all_unresolved"
    assert "sanato.zhang@plaud.ai" in sent_log


@pytest.mark.asyncio
async def test_notify_reviewers_non_ok_reason_falls_back():
    from app.crashguard.services.pr_reviewer import (
        ReviewerResolution, notify_reviewers,
    )
    pr = MagicMock()
    pr.pr_url = "https://github.com/x/y/pull/42"
    pr.pr_number = 42
    pr.repo = "plaud-flutter-global"
    pr.datadog_issue_id = "abc"

    settings = MagicMock()
    settings.pr_reviewer_fallback_email = "sanato.zhang@plaud.ai"

    res = ReviewerResolution(reason="blame_empty")

    sent_log = []

    async def fake_send(chat_id="", card=None, email=""):
        sent_log.append((email, card.get("header", {}).get("template")))
        return True

    with patch("app.services.feishu_cli.send_interactive_card", side_effect=fake_send):
        sent, fb = await notify_reviewers(pr, res, settings)

    assert sent == []
    assert fb == "blame_empty"
    assert ("sanato.zhang@plaud.ai", "orange") in sent_log  # fallback card 用 orange


@pytest.mark.asyncio
async def test_notify_reviewers_skip_fallback_non_ok_no_send():
    """daily sweep 模式：blame 失败时不打扰 sanato。"""
    from app.crashguard.services.pr_reviewer import (
        ReviewerResolution, notify_reviewers,
    )
    pr = MagicMock()
    pr.pr_url = "https://github.com/x/y/pull/42"
    pr.pr_number = 42
    pr.repo = "plaud-flutter-global"
    pr.datadog_issue_id = "abc"

    settings = MagicMock()
    settings.pr_reviewer_fallback_email = "sanato.zhang@plaud.ai"

    res = ReviewerResolution(reason="blame_empty")

    sent_log = []

    async def fake_send(chat_id="", card=None, email=""):
        sent_log.append((email, card.get("header", {}).get("template")))
        return True

    with patch("app.services.feishu_cli.send_interactive_card", side_effect=fake_send):
        sent, fb = await notify_reviewers(pr, res, settings, skip_fallback=True)

    assert sent == []
    assert fb == "blame_empty"
    assert sent_log == []  # 关键：没发任何卡，sanato 不被打扰


@pytest.mark.asyncio
async def test_notify_reviewers_skip_fallback_all_fail_no_send():
    """daily sweep 模式：有 reviewer 但全发失败也不打扰 sanato。"""
    from app.crashguard.services.pr_reviewer import (
        ReviewerResolution, notify_reviewers,
    )
    pr = MagicMock()
    pr.pr_url = "https://github.com/x/y/pull/42"
    pr.pr_number = 42
    pr.repo = "flutter-common"
    pr.datadog_issue_id = "xyz"

    settings = MagicMock()
    settings.pr_reviewer_fallback_email = "sanato.zhang@plaud.ai"

    res = ReviewerResolution(
        emails=["alice@plaud.ai"], line_counts={"alice@plaud.ai": 3},
        reason="ok",
    )

    sent_log = []

    async def fake_send(chat_id="", card=None, email=""):
        sent_log.append(email)
        return False  # 模拟发送失败

    with patch("app.services.feishu_cli.send_interactive_card", side_effect=fake_send):
        sent, fb = await notify_reviewers(pr, res, settings, skip_fallback=True)

    assert sent == []
    assert fb == "all_unresolved"
    assert "sanato.zhang@plaud.ai" not in sent_log  # 没 fallback


# ---------- Task 5: check_review_status_from_gh ----------

def test_check_review_status_merged_returns_true():
    from app.crashguard.services.pr_reviewer import check_review_status_from_gh
    out = json.dumps({
        "state": "MERGED", "mergedAt": "2026-05-21T10:00:00Z",
        "closedAt": None, "reviews": [], "author": {"login": "alice"},
    })
    with patch("subprocess.run", return_value=_fake_run(stdout=out)):
        assert check_review_status_from_gh("https://github.com/x/y/pull/1") is True


def test_check_review_status_closed_returns_true():
    from app.crashguard.services.pr_reviewer import check_review_status_from_gh
    out = json.dumps({
        "state": "CLOSED", "mergedAt": None,
        "closedAt": "2026-05-21T10:00:00Z", "reviews": [], "author": {"login": "alice"},
    })
    with patch("subprocess.run", return_value=_fake_run(stdout=out)):
        assert check_review_status_from_gh("https://github.com/x/y/pull/1") is True


def test_check_review_status_real_human_review_returns_true():
    from app.crashguard.services.pr_reviewer import check_review_status_from_gh
    out = json.dumps({
        "state": "OPEN", "mergedAt": None, "closedAt": None,
        "author": {"login": "alice"},
        "reviews": [{
            "author": {"login": "bob"}, "authorAssociation": "MEMBER",
            "state": "COMMENTED",
        }],
    })
    with patch("subprocess.run", return_value=_fake_run(stdout=out)):
        assert check_review_status_from_gh("https://github.com/x/y/pull/1") is True


def test_check_review_status_bot_review_only_returns_false():
    """Claude/Copilot bot review 不算被 review"""
    from app.crashguard.services.pr_reviewer import check_review_status_from_gh
    out = json.dumps({
        "state": "OPEN", "mergedAt": None, "closedAt": None,
        "author": {"login": "alice"},
        "reviews": [
            {"author": {"login": "claude"}, "authorAssociation": "NONE",
             "state": "COMMENTED"},
            {"author": {"login": "copilot-pull-request-reviewer"},
             "authorAssociation": "NONE", "state": "COMMENTED"},
        ],
    })
    with patch("subprocess.run", return_value=_fake_run(stdout=out)):
        assert check_review_status_from_gh("https://github.com/x/y/pull/1") is False


def test_check_review_status_pr_author_self_comment_returns_false():
    """PR 作者自己 comment 自己 PR 不算 review"""
    from app.crashguard.services.pr_reviewer import check_review_status_from_gh
    out = json.dumps({
        "state": "OPEN", "mergedAt": None, "closedAt": None,
        "author": {"login": "sanatozhang"},
        "reviews": [{
            "author": {"login": "sanatozhang"}, "authorAssociation": "MEMBER",
            "state": "COMMENTED",
        }],
    })
    with patch("subprocess.run", return_value=_fake_run(stdout=out)):
        assert check_review_status_from_gh("https://github.com/x/y/pull/1") is False


def test_check_review_status_none_association_returns_false():
    """authorAssociation=NONE 不算（外部/未关联）"""
    from app.crashguard.services.pr_reviewer import check_review_status_from_gh
    out = json.dumps({
        "state": "OPEN", "mergedAt": None, "closedAt": None,
        "author": {"login": "alice"},
        "reviews": [{
            "author": {"login": "external-user"}, "authorAssociation": "NONE",
            "state": "COMMENTED",
        }],
    })
    with patch("subprocess.run", return_value=_fake_run(stdout=out)):
        assert check_review_status_from_gh("https://github.com/x/y/pull/1") is False


def test_check_review_status_mixed_bot_and_human_returns_true():
    """有 bot 也有真人时——真人的占主导"""
    from app.crashguard.services.pr_reviewer import check_review_status_from_gh
    out = json.dumps({
        "state": "OPEN", "mergedAt": None, "closedAt": None,
        "author": {"login": "alice"},
        "reviews": [
            {"author": {"login": "claude"}, "authorAssociation": "NONE",
             "state": "COMMENTED"},
            {"author": {"login": "alice"}, "authorAssociation": "MEMBER",
             "state": "COMMENTED"},  # PR 作者自己
            {"author": {"login": "bob"}, "authorAssociation": "MEMBER",
             "state": "APPROVED"},   # 真人 review
        ],
    })
    with patch("subprocess.run", return_value=_fake_run(stdout=out)):
        assert check_review_status_from_gh("https://github.com/x/y/pull/1") is True


def test_check_review_status_pr_1071_real_payload_returns_false():
    """覆盖 PR #1071 实际场景：claude bot + copilot + PR 作者自己 ×2 → False"""
    from app.crashguard.services.pr_reviewer import check_review_status_from_gh
    out = json.dumps({
        "state": "OPEN", "mergedAt": None, "closedAt": None, "isDraft": False,
        "author": {"login": "sanatozhang"},
        "reviews": [
            {"author": {"login": "claude"}, "authorAssociation": "NONE",
             "state": "COMMENTED"},
            {"author": {"login": "copilot-pull-request-reviewer"},
             "authorAssociation": "NONE", "state": "COMMENTED"},
            {"author": {"login": "sanatozhang"}, "authorAssociation": "MEMBER",
             "state": "COMMENTED"},
            {"author": {"login": "sanatozhang"}, "authorAssociation": "MEMBER",
             "state": "COMMENTED"},
        ],
    })
    with patch("subprocess.run", return_value=_fake_run(stdout=out)):
        assert check_review_status_from_gh("https://github.com/x/y/pull/1071") is False


def test_check_review_status_gh_failure_returns_false():
    from app.crashguard.services.pr_reviewer import check_review_status_from_gh
    with patch("subprocess.run", return_value=_fake_run(returncode=1, stdout="")):
        assert check_review_status_from_gh("https://github.com/x/y/pull/1") is False


def test_check_review_status_empty_url_returns_false():
    from app.crashguard.services.pr_reviewer import check_review_status_from_gh
    assert check_review_status_from_gh("") is False


# ---------- Task 6: resolve_and_notify orchestrator ----------

@pytest.mark.asyncio
async def test_resolve_and_notify_writes_assigned_at(patched_session):
    from app.crashguard.services import pr_reviewer
    from app.crashguard.services.pr_reviewer import ReviewerResolution
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session

    async with get_session() as s:
        pr = CrashPullRequest(
            analysis_id=1, datadog_issue_id="abc",
            repo="plaud-flutter-global",
            branch_name="crashguard/auto-fix/abc",
            pr_url="https://github.com/Plaud-AI/plaud-flutter-global/pull/999",
            pr_number=999, pr_status="draft",
        )
        s.add(pr)
        await s.commit()
        pid = pr.id

    fake_res = ReviewerResolution(
        emails=["alice@plaud.ai"],
        line_counts={"alice@plaud.ai": 5},
        reason="ok",
    )

    async def fake_send(chat_id="", card=None, email=""):
        return True

    with patch.object(pr_reviewer, "resolve_reviewers_by_blame",
                      return_value=fake_res), \
         patch("app.services.feishu_cli.send_interactive_card",
               side_effect=fake_send):
        result = await pr_reviewer.resolve_and_notify(pid)

    assert result["sent_count"] == 1
    assert result["fallback"] is False
    assert result["reason"] == "ok"

    async with get_session() as s:
        pr2 = await s.get(CrashPullRequest, pid)
        assert pr2.reviewer_assigned_at is not None
        assert pr2.last_reminder_at is not None
        assert "alice@plaud.ai" in (pr2.reviewer_emails or "")
        assert pr2.reviewer_fallback_reason == "ok"


@pytest.mark.asyncio
async def test_resolve_and_notify_fallback_path_writes_reason(patched_session):
    from app.crashguard.services import pr_reviewer
    from app.crashguard.services.pr_reviewer import ReviewerResolution
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session

    async with get_session() as s:
        pr = CrashPullRequest(
            analysis_id=2, datadog_issue_id="def",
            repo="plaud-flutter-global",
            pr_url="https://github.com/Plaud-AI/plaud-flutter-global/pull/998",
            pr_number=998, pr_status="draft",
        )
        s.add(pr)
        await s.commit()
        pid = pr.id

    with patch.object(pr_reviewer, "resolve_reviewers_by_blame",
                      return_value=ReviewerResolution(reason="blame_empty")), \
         patch("app.services.feishu_cli.send_interactive_card",
               return_value=True):
        result = await pr_reviewer.resolve_and_notify(pid)

    assert result["sent_count"] == 0
    assert result["fallback"] is True
    assert result["reason"] == "blame_empty"

    async with get_session() as s:
        pr2 = await s.get(CrashPullRequest, pid)
        assert pr2.reviewer_fallback_reason == "blame_empty"


@pytest.mark.asyncio
async def test_resolve_and_notify_falls_back_to_configured_github_reviewers(patched_session):
    """blame 找不到 owner → 兜底把 pr_reviewer_fallback_github_emails 加为 GH reviewer。"""
    from app.crashguard.services import pr_reviewer
    from app.crashguard.services.pr_reviewer import ReviewerResolution
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session

    async with get_session() as s:
        pr = CrashPullRequest(
            analysis_id=42, datadog_issue_id="nopo",
            repo="plaud-flutter-common",
            pr_url="https://github.com/Plaud-AI/plaud-flutter-common/pull/1225",
            pr_number=1225, pr_status="draft",
        )
        s.add(pr)
        await s.commit()
        pid = pr.id

    calls = []

    def fake_sync(pr_url, emails):
        calls.append(list(emails))
        # 模拟 gavin 加成功、sanato（author）422 失败
        return (["GavinDong-plaud"], ["GavinDong-plaud"], ["sanato.zhang@plaud.ai"])

    with patch.object(pr_reviewer, "resolve_reviewers_by_blame",
                      return_value=ReviewerResolution(reason="blame_empty")), \
         patch.object(pr_reviewer, "sync_github_reviewers_for_emails",
                      side_effect=fake_sync), \
         patch("app.services.feishu_cli.send_interactive_card", return_value=True):
        await pr_reviewer.resolve_and_notify(pid)

    # 主路径（blame 空）不会调用 sync；只有 2.6 兜底调用一次，且用 fallback 邮箱
    assert len(calls) == 1
    assert calls[0] == ["gavin.dong@plaud.ai", "sanato.zhang@plaud.ai"]


@pytest.mark.asyncio
async def test_resolve_and_notify_skips_already_reviewed(patched_session):
    from app.crashguard.services import pr_reviewer
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session

    async with get_session() as s:
        pr = CrashPullRequest(
            analysis_id=3, datadog_issue_id="ghi",
            repo="plaud-flutter-global",
            pr_url="https://github.com/Plaud-AI/plaud-flutter-global/pull/997",
            pr_number=997, pr_status="open",
            reviewed_at=datetime.utcnow(),
        )
        s.add(pr)
        await s.commit()
        pid = pr.id

    with patch.object(pr_reviewer, "resolve_reviewers_by_blame") as m_resolve:
        result = await pr_reviewer.resolve_and_notify(pid)
    m_resolve.assert_not_called()
    assert result["reason"] == "already_reviewed"


@pytest.mark.asyncio
async def test_resolve_and_notify_disabled_short_circuits(patched_session):
    from app.crashguard.services import pr_reviewer
    from app.crashguard.config import get_crashguard_settings
    s = get_crashguard_settings()
    s.pr_reviewer_enabled = False
    try:
        result = await pr_reviewer.resolve_and_notify(999999)
        assert result["reason"] == "disabled"
    finally:
        s.pr_reviewer_enabled = True


@pytest.mark.asyncio
async def test_resolve_and_notify_pr_not_found(patched_session):
    from app.crashguard.services import pr_reviewer
    result = await pr_reviewer.resolve_and_notify(999999)
    assert result["reason"] == "pr_not_found"


# ---------- Task 7: daily_reminder_sweep ----------

@pytest.mark.asyncio
async def test_daily_sweep_skips_already_reminded_today(patched_session):
    from app.crashguard.services import pr_reviewer
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session

    today = datetime.utcnow()
    async with get_session() as s:
        pr = CrashPullRequest(
            analysis_id=10, datadog_issue_id="ddd",
            repo="plaud-flutter-global",
            pr_url="https://github.com/Plaud-AI/plaud-flutter-global/pull/100",
            pr_number=100, pr_status="draft",
            last_reminder_at=today,
        )
        s.add(pr)
        await s.commit()

    with patch.object(pr_reviewer, "resolve_and_notify") as m_notify, \
         patch.object(pr_reviewer, "check_review_status_from_gh", return_value=False):
        result = await pr_reviewer.daily_reminder_sweep()
    m_notify.assert_not_called()
    assert result["skipped_same_day"] >= 1


@pytest.mark.asyncio
async def test_daily_sweep_marks_newly_reviewed(patched_session):
    from app.crashguard.services import pr_reviewer
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session

    async with get_session() as s:
        pr = CrashPullRequest(
            analysis_id=11, datadog_issue_id="eee",
            repo="plaud-flutter-global",
            pr_url="https://github.com/Plaud-AI/plaud-flutter-global/pull/101",
            pr_number=101, pr_status="open",
            last_reminder_at=datetime.utcnow() - timedelta(days=2),
            reviewer_emails='["alice@plaud.ai"]',  # 有明确 assignee 才进 sweep
        )
        s.add(pr)
        await s.commit()
        pid = pr.id

    with patch.object(pr_reviewer, "check_review_status_from_gh", return_value=True), \
         patch.object(pr_reviewer, "resolve_and_notify") as m_notify:
        result = await pr_reviewer.daily_reminder_sweep()

    m_notify.assert_not_called()
    assert result["newly_reviewed"] >= 1

    async with get_session() as s:
        pr2 = await s.get(CrashPullRequest, pid)
        assert pr2.reviewed_at is not None


@pytest.mark.asyncio
async def test_daily_sweep_renotifies_stale(patched_session):
    from app.crashguard.services import pr_reviewer
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session

    async with get_session() as s:
        pr = CrashPullRequest(
            analysis_id=12, datadog_issue_id="fff",
            repo="plaud-flutter-global",
            pr_url="https://github.com/Plaud-AI/plaud-flutter-global/pull/102",
            pr_number=102, pr_status="open",
            last_reminder_at=datetime.utcnow() - timedelta(days=2),
            reviewer_emails='["bob@plaud.ai"]',  # 有明确 assignee 才进 sweep
        )
        s.add(pr)
        await s.commit()

    async def fake_notify(pid, skip_fallback=False):
        return {"sent_count": 1, "fallback": False, "reason": "ok"}

    with patch.object(pr_reviewer, "check_review_status_from_gh", return_value=False), \
         patch.object(pr_reviewer, "resolve_and_notify", side_effect=fake_notify) as m_notify:
        result = await pr_reviewer.daily_reminder_sweep()

    m_notify.assert_called()
    assert result["notified"] >= 1


@pytest.mark.asyncio
async def test_daily_sweep_skips_pr_without_assignee(patched_session):
    """reviewer_emails 空（'[]' / null / 未 blame 过）的 PR 跳过，不打扰兜底人。"""
    from app.crashguard.services import pr_reviewer
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session

    async with get_session() as s:
        # 三种"无 assignee"形态
        for i, emails in enumerate(("[]", "", None)):
            pr = CrashPullRequest(
                analysis_id=20 + i, datadog_issue_id=f"noassign_{i}",
                repo="plaud-flutter-global",
                pr_url=f"https://github.com/Plaud-AI/plaud-flutter-global/pull/{200+i}",
                pr_number=200 + i, pr_status="open",
                last_reminder_at=datetime.utcnow() - timedelta(days=2),
                reviewer_emails=emails,
            )
            s.add(pr)
        await s.commit()

    with patch.object(pr_reviewer, "check_review_status_from_gh", return_value=False), \
         patch.object(pr_reviewer, "resolve_and_notify") as m_notify:
        result = await pr_reviewer.daily_reminder_sweep()

    m_notify.assert_not_called()
    assert result["skipped_no_assignee"] >= 3
    assert result["notified"] == 0


@pytest.mark.asyncio
async def test_daily_sweep_skips_flutter_when_flutter_family_paused(patched_session):
    """2026-07-13：pr_enabled_flutter=False 时，flutter PR 不发 reviewer 提醒
    （3.x 暂停期间不打扰人），但仍照常处理其他 family/未知 family 的 PR。"""
    from app.crashguard.services import pr_reviewer
    from app.crashguard.config import get_crashguard_settings
    from app.crashguard.models import CrashIssue, CrashPullRequest
    from app.db.database import get_session

    async with get_session() as s:
        s.add(CrashIssue(
            datadog_issue_id="flutter_paused", platform="ANDROID", service="plaud-flutter",
            last_seen_version="3.20.0", title="flutter crash", stack_fingerprint="fp_fp",
        ))
        pr = CrashPullRequest(
            analysis_id=30, datadog_issue_id="flutter_paused",
            repo="plaud-android",
            pr_url="https://github.com/Plaud-AI/plaud-android/pull/300",
            pr_number=300, pr_status="open",
            last_reminder_at=datetime.utcnow() - timedelta(days=2),
            reviewer_emails='["carol@plaud.ai"]',
        )
        s.add(pr)
        await s.commit()

    settings = get_crashguard_settings()
    settings.pr_enabled_flutter = False
    settings.pr_enabled_native = True
    try:
        with patch.object(pr_reviewer, "check_review_status_from_gh", return_value=False), \
             patch.object(pr_reviewer, "resolve_and_notify") as m_notify:
            result = await pr_reviewer.daily_reminder_sweep()
    finally:
        settings.pr_enabled_flutter = True

    m_notify.assert_not_called()
    assert result["skipped_family_paused"] >= 1
    assert result["notified"] == 0


@pytest.mark.asyncio
async def test_daily_sweep_skips_reviewed_prs(patched_session):
    """已 reviewed 的不应进入扫描结果"""
    from app.crashguard.services import pr_reviewer
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session

    async with get_session() as s:
        pr = CrashPullRequest(
            analysis_id=13, datadog_issue_id="ggg",
            repo="plaud-flutter-global",
            pr_url="https://github.com/Plaud-AI/plaud-flutter-global/pull/103",
            pr_number=103, pr_status="open",
            reviewed_at=datetime.utcnow(),
        )
        s.add(pr)
        await s.commit()

    with patch.object(pr_reviewer, "check_review_status_from_gh") as m_check, \
         patch.object(pr_reviewer, "resolve_and_notify") as m_notify:
        result = await pr_reviewer.daily_reminder_sweep()

    m_check.assert_not_called()
    m_notify.assert_not_called()
    assert result["processed"] == 0


@pytest.mark.asyncio
async def test_daily_sweep_disabled_returns_zero(patched_session):
    from app.crashguard.services import pr_reviewer
    from app.crashguard.config import get_crashguard_settings
    s = get_crashguard_settings()
    s.pr_reviewer_enabled = False
    try:
        result = await pr_reviewer.daily_reminder_sweep()
        assert result["processed"] == 0
    finally:
        s.pr_reviewer_enabled = True


# ---------- flutter sub-repo URL 解析（治本 bug 修复） ----------

def test_extract_flutter_sub_from_url_global():
    from app.crashguard.services.pr_reviewer import _extract_flutter_sub_from_url
    assert _extract_flutter_sub_from_url(
        "https://github.com/Plaud-AI/plaud-flutter-global/pull/147"
    ) == "global"


def test_extract_flutter_sub_from_url_cn():
    from app.crashguard.services.pr_reviewer import _extract_flutter_sub_from_url
    assert _extract_flutter_sub_from_url(
        "https://github.com/Plaud-AI/plaud-flutter-cn/pull/50"
    ) == "cn"


def test_extract_flutter_sub_from_url_common_returns_empty():
    from app.crashguard.services.pr_reviewer import _extract_flutter_sub_from_url
    assert _extract_flutter_sub_from_url(
        "https://github.com/Plaud-AI/plaud-flutter-common/pull/1096"
    ) == ""


def test_extract_flutter_sub_from_url_non_flutter_returns_empty():
    from app.crashguard.services.pr_reviewer import _extract_flutter_sub_from_url
    assert _extract_flutter_sub_from_url(
        "https://github.com/Plaud-AI/plaud-ios/pull/200"
    ) == ""
    assert _extract_flutter_sub_from_url("") == ""


def test_resolve_repo_path_uses_url_for_flutter_global(monkeypatch):
    """治本验证：pr.repo='flutter' + url 含 plaud-flutter-global → 走 global 仓路径"""
    from app.crashguard.services import pr_reviewer
    captured = {}

    def fake_platform_repo_path(platform, sub_hint=""):
        captured["platform"] = platform
        captured["sub_hint"] = sub_hint
        return f"/fake/{platform}-{sub_hint or 'common'}"

    monkeypatch.setattr(
        "app.crashguard.services.pr_drafter._platform_repo_path",
        fake_platform_repo_path,
    )

    pr = MagicMock()
    pr.repo = "flutter"
    pr.pr_url = "https://github.com/Plaud-AI/plaud-flutter-global/pull/147"
    settings = MagicMock()

    result = pr_reviewer._resolve_repo_path_for_pr(pr, settings)
    assert captured["sub_hint"] == "global"
    assert result == "/fake/flutter-global"


def test_resolve_repo_path_uses_url_for_flutter_common(monkeypatch):
    from app.crashguard.services import pr_reviewer
    captured = {}

    def fake_platform_repo_path(platform, sub_hint=""):
        captured["sub_hint"] = sub_hint
        return f"/fake/{platform}-{sub_hint or 'common'}"

    monkeypatch.setattr(
        "app.crashguard.services.pr_drafter._platform_repo_path",
        fake_platform_repo_path,
    )

    pr = MagicMock()
    pr.repo = "flutter"
    pr.pr_url = "https://github.com/Plaud-AI/plaud-flutter-common/pull/1096"
    settings = MagicMock()

    result = pr_reviewer._resolve_repo_path_for_pr(pr, settings)
    assert captured["sub_hint"] == ""
    assert result == "/fake/flutter-common"


# ============================================================
# C 方案: email → GH login → add-reviewer
# ============================================================
class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_parse_repo_slug_and_pr_number():
    from app.crashguard.services.pr_reviewer import _parse_repo_slug_and_pr_number
    assert _parse_repo_slug_and_pr_number(
        "https://github.com/Plaud-AI/plaud-flutter-common/pull/1190"
    ) == ("Plaud-AI/plaud-flutter-common", 1190)
    assert _parse_repo_slug_and_pr_number("") == ("", 0)
    assert _parse_repo_slug_and_pr_number("not-a-url") == ("", 0)


def test_resolve_email_to_github_login_happy_path(monkeypatch):
    """commits search 返回 login 字符串 → 缓存 + 返回。"""
    from app.crashguard.services import pr_reviewer as pr_mod
    pr_mod._email_to_login_cache.clear()

    def fake_run(args, **kw):
        # 验证调用参数
        assert "search/commits" in " ".join(args)
        assert "luffy@plaud.ai" in " ".join(args)
        return _FakeProc(returncode=0, stdout="luffyYH\n")

    monkeypatch.setattr(pr_mod.subprocess, "run", fake_run)
    login = pr_mod._resolve_email_to_github_login(
        "luffy@plaud.ai", "Plaud-AI/plaud-flutter-common",
    )
    assert login == "luffyYH"
    # cache 命中：第二次调用不应 hit subprocess
    monkeypatch.setattr(pr_mod.subprocess, "run",
                        lambda *a, **kw: pytest.fail("应走 cache 不查 API"))
    assert pr_mod._resolve_email_to_github_login(
        "luffy@plaud.ai", "Plaud-AI/plaud-flutter-common",
    ) == "luffyYH"


def test_resolve_email_to_github_login_not_found_caches_negative(monkeypatch):
    """search 返回空 stdout 表示无匹配 → cache None，下次也不重查。"""
    from app.crashguard.services import pr_reviewer as pr_mod
    pr_mod._email_to_login_cache.clear()
    call_count = {"n": 0}

    def fake_run(*a, **kw):
        call_count["n"] += 1
        return _FakeProc(returncode=0, stdout="")

    monkeypatch.setattr(pr_mod.subprocess, "run", fake_run)
    assert pr_mod._resolve_email_to_github_login(
        "ghost@external.com", "owner/repo",
    ) is None
    # 第二次：应走负缓存
    assert pr_mod._resolve_email_to_github_login(
        "ghost@external.com", "owner/repo",
    ) is None
    assert call_count["n"] == 1, "第二次不应再调 subprocess"


def test_resolve_email_to_github_login_api_failure_returns_none(monkeypatch):
    """gh api 失败 → 返回 None，不抛异常。"""
    from app.crashguard.services import pr_reviewer as pr_mod
    pr_mod._email_to_login_cache.clear()
    monkeypatch.setattr(pr_mod.subprocess, "run",
                        lambda *a, **kw: _FakeProc(returncode=1, stderr="rate limit"))
    assert pr_mod._resolve_email_to_github_login(
        "x@plaud.ai", "owner/repo",
    ) is None


def test_resolve_email_invalid_inputs_returns_none(monkeypatch):
    from app.crashguard.services import pr_reviewer as pr_mod
    assert pr_mod._resolve_email_to_github_login("", "o/r") is None
    assert pr_mod._resolve_email_to_github_login("x@y.com", "") is None
    assert pr_mod._resolve_email_to_github_login("x@y.com", "no-slash") is None


def test_add_github_reviewers_batch_success(monkeypatch):
    """一次 POST 加多个 reviewer，成功路径。"""
    from app.crashguard.services import pr_reviewer as pr_mod
    captured = {}

    def fake_run(args, **kw):
        captured["args"] = list(args)
        return _FakeProc(returncode=0, stdout="{}")

    monkeypatch.setattr(pr_mod.subprocess, "run", fake_run)
    added, failed = pr_mod._add_github_reviewers(
        "https://github.com/Plaud-AI/plaud-flutter-common/pull/1190",
        ["luffyYH", "Victor-Plaud"],
    )
    assert added == ["luffyYH", "Victor-Plaud"]
    assert failed == []
    # 校验 args
    a = captured["args"]
    assert a[:4] == ["gh", "api", "-X", "POST"]
    assert "repos/Plaud-AI/plaud-flutter-common/pulls/1190/requested_reviewers" in a
    # 数组形式：reviewers[]=login 多次
    assert "reviewers[]=luffyYH" in a
    assert "reviewers[]=Victor-Plaud" in a


def test_add_github_reviewers_batch_fail_falls_back_one_by_one(monkeypatch):
    """batch 失败时 fallback 单个加，隔离失败 login。"""
    from app.crashguard.services import pr_reviewer as pr_mod
    call_count = {"n": 0}

    def fake_run(args, **kw):
        call_count["n"] += 1
        # 第 1 次（batch）失败；第 2 次（fallback A）成功；第 3 次（fallback B）422
        if call_count["n"] == 1:
            return _FakeProc(returncode=1, stderr="HTTP 422")
        if "reviewers[]=goodLogin" in args:
            return _FakeProc(returncode=0)
        return _FakeProc(returncode=1, stderr="HTTP 422: not a collaborator")

    monkeypatch.setattr(pr_mod.subprocess, "run", fake_run)
    added, failed = pr_mod._add_github_reviewers(
        "https://github.com/o/r/pull/1",
        ["goodLogin", "badLogin"],
    )
    assert added == ["goodLogin"]
    assert failed == ["badLogin"]


def test_add_github_reviewers_invalid_pr_url(monkeypatch):
    """非法 PR URL 直接返回，不调 API。"""
    from app.crashguard.services import pr_reviewer as pr_mod
    monkeypatch.setattr(pr_mod.subprocess, "run",
                        lambda *a, **kw: pytest.fail("不应调 API"))
    added, failed = pr_mod._add_github_reviewers("not-a-url", ["a", "b"])
    assert added == []
    assert failed == ["a", "b"]


def test_add_github_reviewers_empty_list_noop(monkeypatch):
    from app.crashguard.services import pr_reviewer as pr_mod
    monkeypatch.setattr(pr_mod.subprocess, "run",
                        lambda *a, **kw: pytest.fail("不应调 API"))
    assert pr_mod._add_github_reviewers("https://github.com/o/r/pull/1", []) == ([], [])


def test_sync_github_reviewers_full_chain(monkeypatch):
    """端到端：emails → resolve login → add-reviewer。"""
    from app.crashguard.services import pr_reviewer as pr_mod
    pr_mod._email_to_login_cache.clear()

    # mock resolve: alice→Alice; bob→None (ghost)
    monkeypatch.setattr(pr_mod, "_resolve_email_to_github_login",
                        lambda email, repo, timeout=15:
                        {"alice@plaud.ai": "Alice"}.get(email))
    # mock add: 成功
    monkeypatch.setattr(pr_mod, "_add_github_reviewers",
                        lambda url, logins, timeout=30: (logins, []))

    resolved, added, failed = pr_mod.sync_github_reviewers_for_emails(
        "https://github.com/o/r/pull/1",
        ["alice@plaud.ai", "bob@plaud.ai"],
    )
    assert resolved == ["Alice"]
    assert added == ["Alice"]
    # bob 反查失败 → 进 failed
    assert "bob@plaud.ai" in failed


def test_sync_github_reviewers_no_login_resolved_skips_add(monkeypatch):
    """全部 email 反查失败时不调 _add_github_reviewers。"""
    from app.crashguard.services import pr_reviewer as pr_mod
    monkeypatch.setattr(pr_mod, "_resolve_email_to_github_login",
                        lambda *a, **kw: None)
    called = {"add": False}

    def fake_add(*a, **kw):
        called["add"] = True
        return ([], [])

    monkeypatch.setattr(pr_mod, "_add_github_reviewers", fake_add)
    resolved, added, failed = pr_mod.sync_github_reviewers_for_emails(
        "https://github.com/o/r/pull/1", ["ghost@x.com"],
    )
    assert resolved == [] and added == []
    assert failed == ["ghost@x.com"]
    assert called["add"] is False


# ============================================================
# 方案 A: GitHub 指派与 @plaud.ai 域名白名单解耦
#
# 抓手：48 条自动 PR 里 16 条 reason=bot_only —— blame 出来的真实作者用
# 个人/构建机 commit 邮箱（492934747@qq.com / root@kaaaaai.cn），被
# pr_reviewer_allowed_email_domains=["plaud.ai"] 全削光 → 既不发飞书也不
# 指派 GitHub reviewer。域名白名单本是「飞书按邮箱直发」的路由约束，不该
# 连带掐死「GitHub add-reviewer」（后者用 commit email→GH login，与域名无关）。
# ============================================================

def _blame_run_factory(diff, email_by_line):
    """构造 subprocess.run 替身：gh 返回 diff，blame 按 -L 行号返回对应 author。"""
    def fake_run(cmd, **kw):
        if cmd[0] == "gh":
            return _fake_run(stdout=diff)
        if "blame" in cmd:
            # cmd: git blame -L {ln},{ln} --porcelain HEAD -- file
            spec = cmd[cmd.index("-L") + 1]
            ln = int(spec.split(",")[0])
            em = email_by_line.get(ln, "")
            if not em:
                return _fake_run(returncode=1)
            porc = (
                f"abc 1 1 1\nauthor X\nauthor-mail <{em}>\n"
                "summary s\n\tcode\n"
            )
            return _fake_run(stdout=porc)
        return _fake_run(returncode=1)
    return fake_run


def test_resolve_reviewers_github_candidates_ignore_domain_whitelist(tmp_path):
    """ok 路径：feishu emails 受域名白名单约束，github_candidate_emails 不受。"""
    from app.crashguard.services.pr_reviewer import resolve_reviewers_by_blame
    settings = MagicMock()
    settings.pr_reviewer_blocked_authors = []
    settings.pr_reviewer_top_n = 5
    settings.pr_reviewer_min_lines_pct = 0.0
    settings.pr_reviewer_allowed_email_domains = ["plaud.ai"]

    # 3 个改动行：1 个 plaud 作者 + 1 个 qq 作者
    diff = (
        "--- a/x.dart\n+++ b/x.dart\n"
        "@@ -1,2 +1,2 @@\n-l1\n-l2\n+n1\n+n2\n"
    )
    email_by_line = {1: "alice@plaud.ai", 2: "727732656@qq.com"}

    with patch("subprocess.run", side_effect=_blame_run_factory(diff, email_by_line)):
        r = resolve_reviewers_by_blame("https://github.com/x/y/pull/1",
                                        str(tmp_path), settings)

    assert r.reason == "ok"
    assert r.emails == ["alice@plaud.ai"]                 # 飞书路径：白名单生效
    assert set(r.github_candidate_emails) == {            # GH 路径：不过白名单
        "alice@plaud.ai", "727732656@qq.com",
    }


def test_resolve_reviewers_bot_only_still_yields_github_candidates(tmp_path):
    """bot_only：白名单削光飞书 emails，但 github_candidate_emails 仍保留真实作者。"""
    from app.crashguard.services.pr_reviewer import resolve_reviewers_by_blame
    settings = MagicMock()
    settings.pr_reviewer_blocked_authors = ["crashguard-bot@plaud.ai"]
    settings.pr_reviewer_top_n = 2
    settings.pr_reviewer_min_lines_pct = 0.0
    settings.pr_reviewer_allowed_email_domains = ["plaud.ai"]

    diff = (
        "--- a/x.dart\n+++ b/x.dart\n"
        "@@ -1,2 +1,2 @@\n-l1\n-l2\n+n1\n+n2\n"
    )
    email_by_line = {1: "492934747@qq.com", 2: "root@kaaaaai.cn"}

    with patch("subprocess.run", side_effect=_blame_run_factory(diff, email_by_line)):
        r = resolve_reviewers_by_blame("https://github.com/x/y/pull/1",
                                        str(tmp_path), settings)

    assert r.reason == "bot_only"     # 飞书路径：无可发对象
    assert r.emails == []
    assert set(r.github_candidate_emails) == {"492934747@qq.com", "root@kaaaaai.cn"}


def test_resolve_reviewers_github_candidates_still_respect_blocklist(tmp_path):
    """blocked authors（bot 自身）仍要从 github_candidate_emails 里剔除。"""
    from app.crashguard.services.pr_reviewer import resolve_reviewers_by_blame
    settings = MagicMock()
    settings.pr_reviewer_blocked_authors = ["crashguard-bot@plaud.ai"]
    settings.pr_reviewer_top_n = 5
    settings.pr_reviewer_min_lines_pct = 0.0
    settings.pr_reviewer_allowed_email_domains = ["plaud.ai"]

    diff = (
        "--- a/x.dart\n+++ b/x.dart\n"
        "@@ -1,2 +1,2 @@\n-l1\n-l2\n+n1\n+n2\n"
    )
    email_by_line = {1: "crashguard-bot@plaud.ai", 2: "727732656@qq.com"}

    with patch("subprocess.run", side_effect=_blame_run_factory(diff, email_by_line)):
        r = resolve_reviewers_by_blame("https://github.com/x/y/pull/1",
                                        str(tmp_path), settings)

    # bot 被 block；qq 真实作者保留为 GH 候选
    assert r.github_candidate_emails == ["727732656@qq.com"]


@pytest.mark.asyncio
async def test_resolve_and_notify_github_sync_decoupled_from_feishu(patched_session):
    """核心修复：bot_only（飞书 emails 空）时，GitHub add-reviewer 仍按
    github_candidate_emails 执行，不再被 `sent` 为空掐死。"""
    from app.crashguard.services import pr_reviewer
    from app.crashguard.services.pr_reviewer import ReviewerResolution
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session

    async with get_session() as s:
        pr = CrashPullRequest(
            analysis_id=77, datadog_issue_id="botonly",
            repo="plaud-android",
            pr_url="https://github.com/Plaud-AI/plaud-android/pull/262",
            pr_number=262, pr_status="open",
        )
        s.add(pr)
        await s.commit()
        pid = pr.id

    # blame 全是非 plaud 作者 → 飞书 emails 空、reason=bot_only，但 GH 候选有人
    res = ReviewerResolution(
        emails=[], line_counts={}, reason="bot_only",
        github_candidate_emails=["492934747@qq.com"],
    )

    sync_calls = []

    def fake_sync(pr_url, emails):
        sync_calls.append((pr_url, list(emails)))
        return (["realDevLogin"], ["realDevLogin"], [])

    with patch.object(pr_reviewer, "resolve_reviewers_by_blame", return_value=res), \
         patch.object(pr_reviewer, "sync_github_reviewers_for_emails",
                      side_effect=fake_sync), \
         patch("app.services.feishu_cli.send_interactive_card", return_value=True):
        await pr_reviewer.resolve_and_notify(pid, skip_fallback=True)

    # 关键断言：即便飞书没发出任何卡，GitHub 指派仍按 GH 候选触发
    assert sync_calls == [(
        "https://github.com/Plaud-AI/plaud-android/pull/262",
        ["492934747@qq.com"],
    )]
