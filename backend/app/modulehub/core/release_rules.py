"""Request validation. The version rule itself lives in module-kit; modulehub never computes versions."""
from __future__ import annotations


class InvalidRequest(ValueError):
    pass


def is_release_branch(branch: str) -> bool:
    return branch.startswith("release/") and len(branch) > len("release/")


def validate_request(branch: str, major: bool) -> None:
    if not (branch == "main" or is_release_branch(branch)):
        raise InvalidRequest("branch must be main or release/*: %r" % branch)
    if major and is_release_branch(branch):
        raise InvalidRequest("major bump is not allowed on release/*")


def validate_platform(platform: str) -> None:
    if platform not in ("android", "ios"):
        raise InvalidRequest("platform must be android or ios: %r" % platform)


def module_repo(owner: str, name: str, platform: str) -> str:
    return "%s/plaud-module-%s-%s" % (owner, name, platform)
