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


def test_platform_validation():
    r.validate_platform("android"); r.validate_platform("ios")
    with pytest.raises(r.InvalidRequest):
        r.validate_platform("harmony")


def test_repo_names():
    assert r.module_repo("Plaud-AI", "logger", "android") == "Plaud-AI/logger-android"
    assert r.module_repo("Plaud-AI", "logger", "ios") == "Plaud-AI/logger-ios"


def test_is_release_branch():
    assert r.is_release_branch("release/x") and not r.is_release_branch("main")
