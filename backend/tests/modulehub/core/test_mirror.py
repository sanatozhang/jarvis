from app.modulehub.core import mirror as m


def test_creates_missing_branches_from_the_pinned_tag():
    plan = m.plan_mirror(
        pins={"release/1.0": {"android": "1.2.0", "ios": "1.2.0"}, "release/1.1": {"android": "1.3.0"}, "feature/x": {"ios": "1.0.0"}},
        module_branches=["main", "release/1.0"],
    )
    assert plan == [m.Action("verify", "release/1.0", "v1.2.0"), m.Action("create", "release/1.1", "v1.3.0")]


def test_different_pins_cut_from_the_newest_and_list_the_older_ones():
    plan = m.plan_mirror(pins={"release/2.0": {"android": "1.10.0", "ios": "1.9.0"}}, module_branches=[])
    assert plan == [m.Action("create", "release/2.0", "v1.10.0", (("v1.9.0", ("ios",)),))]


def test_branch_without_pin_is_reported_not_created():
    plan = m.plan_mirror(pins={"release/2.0": {"android": None, "ios": None}}, module_branches=[])
    assert plan == [m.Action("no_pin", "release/2.0")]


def test_only_release_branches_are_mirrored():
    assert m.plan_mirror(pins={"main": {"android": "1.0.0"}, "fix/a/b": {"ios": "1.0.0"}}, module_branches=[]) == []


def test_order_is_stable_and_sorted():
    plan = m.plan_mirror(pins={"release/b": {"android": "1.0.0"}, "release/a": {"ios": "1.0.0"}}, module_branches=[])
    assert [a.branch for a in plan] == ["release/a", "release/b"]
