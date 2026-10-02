from app.modulehub.core import mirror as m


def test_creates_missing_branches_from_pinned_tag():
    plan = m.plan_mirror(
        shell_branches=["main", "release/1.0", "release/1.1", "feature/x"],
        module_branches=["main", "release/1.0"],
        pins={"release/1.0": "1.2.0", "release/1.1": "1.3.0"},
    )
    assert plan == [
        m.Action("verify", "release/1.0", "v1.2.0"),
        m.Action("create", "release/1.1", "v1.3.0"),
    ]


def test_branch_without_pin_is_reported_not_created():
    plan = m.plan_mirror(shell_branches=["release/2.0"], module_branches=[], pins={})
    assert plan == [m.Action("no_pin", "release/2.0", "")]


def test_only_release_branches_are_mirrored():
    assert m.plan_mirror(shell_branches=["main", "fix/a/b"], module_branches=[], pins={}) == []


def test_order_is_stable_and_sorted():
    plan = m.plan_mirror(shell_branches=["release/b", "release/a"], module_branches=[],
                         pins={"release/a": "1.0.0", "release/b": "1.0.0"})
    assert [a.branch for a in plan] == ["release/a", "release/b"]
