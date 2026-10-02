from app.modulehub.core import backport as b


def test_no_backport_for_main():
    assert b.plan_backport("logger", "main", "1.1.0", "1.0.0", already_on_main=False) is None


def test_no_backport_when_already_on_main():
    assert b.plan_backport("logger", "release/1.0", "1.0.1", "1.0.0", already_on_main=True) is None


def test_plan_for_release_branch():
    p = b.plan_backport("logger", "release/1.0", "1.0.1", "1.0.0", already_on_main=False)
    assert p.branch == "chore/jarvis/backport-logger-1.0.1"
    assert p.base == "main"
    assert p.commit_range == "v1.0.0..v1.0.1"
    assert "1.0.1" in p.title and "release/1.0" in p.body


def test_first_release_without_previous_tag_uses_tag_only():
    p = b.plan_backport("logger", "release/1.0", "1.0.0", "-", already_on_main=False)
    assert p.commit_range == "v1.0.0"
