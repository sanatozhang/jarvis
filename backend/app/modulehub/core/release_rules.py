"""Request validation. The version rule itself lives in module-kit; modulehub never computes versions."""
from __future__ import annotations

from typing import Iterable, List

PLATFORMS = ("android", "ios")


class InvalidRequest(ValueError):
    pass


def is_release_branch(branch: str) -> bool:
    return branch.startswith("release/") and len(branch) > len("release/")


def validate_request(branch: str, major: bool) -> None:
    if not (branch == "main" or is_release_branch(branch)):
        raise InvalidRequest("branch must be main or release/*: %r" % branch)
    if major and is_release_branch(branch):
        raise InvalidRequest("major bump is not allowed on release/*")


def normalize_platforms(platforms: Iterable[str]) -> List[str]:
    """Non-empty subset of android/ios in canonical order (the order the job runs them)."""
    wanted = list(platforms)
    bad = [p for p in wanted if p not in PLATFORMS]
    if bad:
        raise InvalidRequest("platforms must be android and/or ios: %r" % bad)
    out = [p for p in PLATFORMS if p in wanted]
    if not out:
        raise InvalidRequest("select at least one platform")
    return out


def module_repo(owner: str, name: str) -> str:
    """One repository per module, holding every platform (android/, ios/)."""
    return "%s/%s" % (owner, name)


def semver_key(version: str):
    return tuple(int(x) for x in version.split("."))
