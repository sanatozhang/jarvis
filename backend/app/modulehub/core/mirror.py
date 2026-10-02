"""Release branch reconciliation plan (contract: module-kit docs/orchestrator/branch-mirror.md).

One module repo serves both shells. On a release branch both shells pin a version of the module; the module branch
is cut from the newest pinned tag and must contain every pinned tag. When the pins differ, the service must also
confirm that the older pins' platforms did not change in between (`older`), otherwise the branch is `diverged`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from app.modulehub.core.release_rules import is_release_branch, semver_key


@dataclass(frozen=True)
class Action:
    kind: str                                         # create | verify | no_pin
    branch: str
    tag: str = ""                                     # newest pinned tag ("" for no_pin)
    older: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()  # (older pinned tag, platforms pinning it)


def plan_mirror(*, pins: Dict[str, Dict[str, Optional[str]]], module_branches: List[str]) -> List[Action]:
    """`pins`: release branch -> {platform: pinned version or None} for every shell that has the branch."""
    existing = set(module_branches)
    actions: List[Action] = []
    for branch in sorted(b for b in pins if is_release_branch(b)):
        by_version: Dict[str, List[str]] = {}
        for platform, version in sorted(pins[branch].items()):
            if version:
                by_version.setdefault(version, []).append(platform)
        if not by_version:
            actions.append(Action("no_pin", branch))
            continue
        ordered = sorted(by_version, key=semver_key)
        newest = ordered[-1]
        older = tuple(("v" + v, tuple(by_version[v])) for v in ordered[:-1])
        actions.append(Action("verify" if branch in existing else "create", branch, "v" + newest, older))
    return actions
