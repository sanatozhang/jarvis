import pytest

from app.modulehub.core import release_rules as r


def test_branch_must_be_main_or_release():
    r.validate_request("main", False)
    r.validate_request("release/4.0.3_0925", False)
    for bad in ("feature/a/b", "dev", "", "release", "release/"):
        with pytest.raises(r.InvalidRequest):
            r.validate_request(bad, False)


def test_major_not_allowed_on_release():
    r.validate_request("main", True)
    with pytest.raises(r.InvalidRequest):
        r.validate_request("release/1.0", True)


def test_platforms_are_normalized():
    assert r.normalize_platforms(["ios", "android", "ios"]) == ["android", "ios"]
    assert r.normalize_platforms(["ios"]) == ["ios"]
    for bad in (["harmony"], [], ["android", "web"]):
        with pytest.raises(r.InvalidRequest):
            r.normalize_platforms(bad)


def test_module_repo_comes_from_the_shells_and_must_agree():
    assert r.module_repo("logger", {"android": "Plaud-AI/mobile_logger", "ios": "Plaud-AI/mobile_logger"}) == "Plaud-AI/mobile_logger"
    for bad in ({}, {"android": ""}, {"android": "Plaud-AI/a", "ios": "Plaud-AI/b"}):
        with pytest.raises(r.InvalidRequest):
            r.module_repo("logger", bad)


def test_semver_order():
    assert sorted(["1.10.0", "1.9.0", "1.9.10"], key=r.semver_key) == ["1.9.0", "1.9.10", "1.10.0"]


def test_is_release_branch():
    assert r.is_release_branch("release/x") and not r.is_release_branch("main")
