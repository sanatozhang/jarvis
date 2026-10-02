"""Release -> main backport plan (contract: backport-pr.md)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.modulehub.core import naming
from app.modulehub.core.release_rules import is_release_branch


@dataclass(frozen=True)
class BackportPlan:
    branch: str
    base: str
    commit_range: str
    title: str
    body: str


def plan_backport(name: str, release_branch: str, version: str, previous_version: str,
                  *, already_on_main: bool) -> Optional[BackportPlan]:
    if not is_release_branch(release_branch) or already_on_main:
        return None
    rng = "v%s" % version if previous_version in ("", "-") else "v%s..v%s" % (previous_version, version)
    return BackportPlan(
        branch=naming.backport_branch(name, version),
        base="main",
        commit_range=rng,
        title="chore: backport %s %s from %s" % (name, version, release_branch),
        body="Carries the commits of `%s` (%s) into `main`.\n\nOn conflict this PR is opened empty; resolve manually.\n" % (release_branch, rng),
    )
