"""Release branch reconciliation plan (contract: branch-mirror.md)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from app.modulehub.core.release_rules import is_release_branch


@dataclass(frozen=True)
class Action:
    kind: str      # create | verify | no_pin
    branch: str
    tag: str       # "v<version>" or "" for no_pin


def plan_mirror(*, shell_branches: List[str], module_branches: List[str], pins: Dict[str, Optional[str]]) -> List[Action]:
    existing = set(module_branches)
    actions: List[Action] = []
    for branch in sorted(b for b in shell_branches if is_release_branch(b)):
        version = pins.get(branch)
        if not version:
            actions.append(Action("no_pin", branch, ""))
        else:
            actions.append(Action("verify" if branch in existing else "create", branch, "v" + version))
    return actions
