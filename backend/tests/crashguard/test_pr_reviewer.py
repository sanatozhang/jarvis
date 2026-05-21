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
